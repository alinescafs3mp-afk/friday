"""One-snapshot, principal-authorized lexical recall over private messages.

The caller owns the SQLite transaction.  This module neither starts nor ends a
transaction and performs no writes, so cancellation can only abort read work.
Returned values contain message bodies and storage identities; they are
process-private inputs to the archive authority layer, never public payloads.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import InitVar, dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn, SupportsIndex, TypeAlias

from friday.retrieval.archive_search_contract import ConversationScope
from friday.retrieval.contracts import LifecycleState, MessageRole
from friday.storage._knowledge import _fts_term_groups

_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{16}\Z")
_CONVERSATION_ID = re.compile(r"conv_[0-9a-f]{16}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FACTORY = object()
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
    return parsed.astimezone(UTC).isoformat()


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
            or type(self.conversation_archived) is not bool
        ):
            raise ArchiveMessageStorageError("message ledger evidence is invalid")

    def __repr__(self) -> str:
        return f"ArchiveMessageLedgerEvidence(row_count={self.row_count}, private=True)"


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

    scope: ArchiveMessageScope
    hits: tuple[ArchiveMessageHit, ...]
    ledgers: tuple[ArchiveMessageLedgerEvidence, ...]
    roles: tuple[MessageRole, ...]
    lifecycle_states: tuple[LifecycleState, ...]
    since: str | None
    until: str | None
    total: int
    examined: int
    has_more: bool
    boundary_identity_sha256: str | None
    _factory: InitVar[object] = None

    def __post_init__(self, _factory: object) -> None:
        if (
            _factory is not _FACTORY
            or type(self.scope) is not ArchiveMessageScope
            or type(self.hits) is not tuple
            or any(type(item) is not ArchiveMessageHit for item in self.hits)
            or tuple(item.match_rank for item in self.hits) != tuple(range(1, len(self.hits) + 1))
            or type(self.ledgers) is not tuple
            or any(type(item) is not ArchiveMessageLedgerEvidence for item in self.ledgers)
            or type(self.roles) is not tuple
            or any(type(item) is not MessageRole for item in self.roles)
            or type(self.lifecycle_states) is not tuple
            or self.lifecycle_states != _lifecycle_states(self.lifecycle_states)
            or isinstance(self.total, bool)
            or isinstance(self.examined, bool)
            or self.total < len(self.hits)
            or self.examined < self.total
            or type(self.has_more) is not bool
            or self.has_more is not (self.total > len(self.hits))
            or (
                self.boundary_identity_sha256 is not None
                and _SHA256.fullmatch(self.boundary_identity_sha256) is None
            )
        ):
            raise ArchiveMessageStorageError("message page evidence is invalid")

    @property
    def returned(self) -> int:
        return len(self.hits)

    def __repr__(self) -> str:
        return (
            "ArchiveMessageSearchPage("
            f"scope={self.scope.value!r}, returned={self.returned}, total={self.total}, "
            f"examined={self.examined}, has_more={self.has_more}, private=True)"
        )


def _records(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = tuple(item[0] for item in (cursor.description or ()))
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


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
    __slots__ = ("archived", "count", "first", "hasher", "last")

    def __init__(self) -> None:
        self.hasher = hashlib.sha256()
        self.hasher.update(b'{"row_identity_sha256s":[')
        self.count = 0
        self.first = ""
        self.last = ""
        self.archived: bool | None = None

    def add(self, row: ArchiveMessageRow) -> None:
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

    def finish(self) -> str:
        self.hasher.update(b'],"schema":"friday.private-message-window-row-ledger.v1"}')
        return self.hasher.hexdigest()


def _ledger_evidence(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    conversation_ids: tuple[str, ...],
    scope: ArchiveMessageScope,
    since: str | None,
    until: str | None,
    boundary_conversation_id: str | None,
    boundary_rowid: int | None,
    boundary_identity_sha256: str | None,
) -> tuple[ArchiveMessageLedgerEvidence, ...]:
    placeholders = ",".join("?" for _item in conversation_ids)
    statement = f"""WITH selected_owned AS MATERIALIZED (
                         SELECT c.id, c.user_id, c.is_archived
                           FROM conversations c
                          WHERE c.user_id=? AND c.id IN ({placeholders})
                     )
                     SELECT m.id AS ledger_id,
                            m.conversation_id AS ledger_conversation_id,
                            m.user_id AS ledger_principal_id,
                            m.role AS ledger_role,
                            m.content AS ledger_content,
                            m.created_at AS ledger_created_at,
                            c.is_archived AS ledger_conversation_archived
                       FROM messages m
                       JOIN selected_owned c
                         ON c.id=m.conversation_id AND c.user_id=m.user_id
                      WHERE m.user_id=?
                        AND m.role IN ('user', 'assistant')
                        AND (? IS NULL OR julianday(m.created_at) >= julianday(?))
                        AND (? IS NULL OR julianday(m.created_at) < julianday(?))
                        AND (?='all' OR (m.conversation_id=? AND m.rowid<?))
                      ORDER BY m.conversation_id ASC,
                               julianday(m.created_at) ASC, m.rowid ASC"""  # nosec B608
    cursor = conn.execute(
        statement,
        (
            principal_id,
            *conversation_ids,
            principal_id,
            since,
            since,
            until,
            until,
            scope.value,
            boundary_conversation_id,
            boundary_rowid,
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
            accumulator.add(row)

    evidence: list[ArchiveMessageLedgerEvidence] = []
    for conversation_id in conversation_ids:
        accumulator = accumulators[conversation_id]
        if accumulator.count == 0 or accumulator.archived is None:
            raise ArchiveMessageStorageError("message ledger could not be reselected")
        evidence.append(
            ArchiveMessageLedgerEvidence(
                conversation_id=conversation_id,
                row_ledger_sha256=accumulator.finish(),
                boundary_identity_sha256=boundary_identity_sha256,
                row_count=accumulator.count,
                first_message_id=accumulator.first,
                last_message_id=accumulator.last,
                conversation_archived=accumulator.archived,
                _factory=_FACTORY,
            )
        )
    return tuple(evidence)


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

    ``None`` is reserved for a missing/foreign current-conversation boundary.
    An authorized search with zero matches returns an empty typed page.
    """

    principal = _private_scope(principal_id, label="principal identity")
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
    if scope is ArchiveMessageScope.CURRENT:
        current_conversation = _conversation_id(conversation_id, label="current conversation identity")
        boundary_id = _message_id(boundary_user_message_id, label="current message boundary")
    else:
        if conversation_id is not None or boundary_user_message_id is not None:
            raise ArchiveMessageStorageError("all-conversation scope cannot carry a current boundary")
        current_conversation = None
        boundary_id = None
    include_user = int(MessageRole.USER in selected_roles)
    include_assistant = int(MessageRole.ASSISTANT in selected_roles)
    include_active = int(LifecycleState.ACTIVE in selected_lifecycles)
    include_archived = int(LifecycleState.ARCHIVED in selected_lifecycles)

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
                    WHERE ?='current' AND b.id=? AND b.user_id=? AND b.role='user'
                   UNION ALL
                   SELECT NULL, NULL, NULL, NULL, NULL, NULL WHERE ?='all'
               ),
               authorized_context AS MATERIALIZED (
                   SELECT m.id, m.conversation_id, m.user_id, m.role, m.content,
                          m.created_at, m.rowid AS message_rowid,
                          c.is_archived AS conversation_archived,
                          ROW_NUMBER() OVER (
                              PARTITION BY m.conversation_id
                              ORDER BY julianday(m.created_at) ASC, m.rowid ASC
                          ) AS conversation_sequence
                     FROM messages m
                     JOIN eligible_conversations c
                       ON c.id=m.conversation_id AND c.user_id=m.user_id
                     CROSS JOIN scope_gate gate
                    WHERE m.user_id=?
                      AND m.role IN ('user', 'assistant')
                      AND (? IS NULL OR julianday(m.created_at) >= julianday(?))
                      AND (? IS NULL OR julianday(m.created_at) < julianday(?))
                      AND (?='all' OR (
                           m.conversation_id=gate.boundary_conversation_id
                           AND m.rowid<gate.boundary_rowid
                      ))
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
                               OR is_archived NOT IN (0, 1)) AS invalid_lifecycle
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
            scope.value,
            boundary_id,
            principal,
            scope.value,
            principal,
            start,
            start,
            end,
            end,
            scope.value,
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
    examined = int(first["examined"])
    total = int(first["total"])
    boundary_digest = _boundary_identity(first) if scope is ArchiveMessageScope.CURRENT else None
    boundary_rowid = int(first["boundary_rowid"]) if scope is ArchiveMessageScope.CURRENT else None
    boundary_conversation = (
        _conversation_id(first["boundary_conversation_id"], label="stored boundary conversation")
        if scope is ArchiveMessageScope.CURRENT
        else None
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
            since=start,
            until=end,
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
        scope=scope,
        hits=tuple(hits),
        ledgers=ledgers,
        roles=selected_roles,
        lifecycle_states=selected_lifecycles,
        since=start,
        until=end,
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

    ``None`` is reserved for a missing/foreign current-conversation boundary.
    An authorized search with zero matches returns an empty typed page.  The
    archive authority callback must rerun this exact selector, with the same
    normalized controls, inside its publication ``BEGIN IMMEDIATE`` and compare
    the resulting private candidate and coverage evidence.  A returned page is
    deliberately not a transferable authorization proof by itself.  The adapter
    must separately attest the derivative FTS build before declaring coverage
    complete; ``has_more`` without a frozen tail is capped coverage, not a cursor.
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
    "ArchiveMessageRow",
    "ArchiveMessageScope",
    "ArchiveMessageSearchPage",
    "ArchiveMessageStorageError",
    "select_authorized_archive_message_page_in_transaction",
]
