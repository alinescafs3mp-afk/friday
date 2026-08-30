"""Bounded restart-safe writer for conversation-passage projections.

The authoritative ``messages`` rows remain the only body store.  This module
advances the body-free sidecar by at most one anchor per work item and updates
the guarded parent after every append.  The parent count is therefore the
durable cursor: replay after a crash either observes the old prefix or resumes
from the already-committed next ordinal.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from friday.conversation_passages.contract import (
    CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256,
    CONVERSATION_PASSAGE_EMPTY_SET_SHA256,
    CONVERSATION_PASSAGE_INDEX_REVISION,
)
from friday.conversation_passages.schema import (
    CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES,
    conversation_passage_anchor_locator_sha256,
    conversation_passage_content_sha256,
    conversation_passage_message_revision_sha256,
    conversation_passage_prefix_sha256,
    conversation_passage_set_extend_sha256,
    validate_conversation_passage_schema,
)
from friday.user_ids import validate_user_id

CONVERSATION_PASSAGE_DEFAULT_WORK_ITEMS = 64
CONVERSATION_PASSAGE_MAX_WORK_ITEMS = 256
CONVERSATION_PASSAGE_MAX_ACTIVE_CONVERSATIONS = 32

_CONVERSATION_ID = re.compile(r"conv_[0-9a-f]{16}\Z")
_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{16}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SCAN_CURSOR = re.compile(
    r"cpw2:(-|conv_[0-9a-f]{16}):([01]):([01]):([01]):"
    r"(-|conv_[0-9a-f]{16}):([01]):([01]):([01]):([sb])\Z"
)
_RETRYABLE_REASONS = frozenset({"backfill_pending", "source_changed"})


@dataclass(frozen=True, slots=True)
class _PublishStep:
    action: str
    source_bytes: int = 0
    anchor_written: bool = False


@dataclass(frozen=True, slots=True)
class _ReasonScanCursor:
    last_conversation_id: str | None = None
    cycle_dirty: bool = False
    wrap_next: bool = False
    resume_inclusive: bool = False


@dataclass(frozen=True, slots=True)
class _OwnerScanCursor:
    source_changed: _ReasonScanCursor = _ReasonScanCursor()
    backfill_pending: _ReasonScanCursor = _ReasonScanCursor()
    next_reason: str = "source_changed"


@dataclass(frozen=True, slots=True)
class _ReasonScanPage:
    reason: str
    raw_ids: tuple[str, ...]
    owned_ids: tuple[str, ...]
    cycle_dirty: bool
    has_raw_tail: bool


def _bounded_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= CONVERSATION_PASSAGE_MAX_WORK_ITEMS:
        raise ValueError(
            f"conversation passage limit must be between 1 and {CONVERSATION_PASSAGE_MAX_WORK_ITEMS}"
        )
    return value


def _bounded_cursor(value: object) -> _OwnerScanCursor:
    if value is None:
        return _OwnerScanCursor()
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("conversation passage cursor must be exact TEXT or None")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ValueError("conversation passage cursor must be exact TEXT or None") from None
    if len(encoded) > 200 or any(ord(character) < 32 for character in value):
        raise ValueError("conversation passage cursor must be exact TEXT or None")
    if _CONVERSATION_ID.fullmatch(value) is not None:
        # Compatibility with the original private inclusive cursor.
        inclusive = _ReasonScanCursor(
            last_conversation_id=value,
            resume_inclusive=True,
        )
        return _OwnerScanCursor(
            source_changed=inclusive,
            backfill_pending=inclusive,
        )
    match = _SCAN_CURSOR.fullmatch(value)
    if match is None:
        raise ValueError("conversation passage cursor must be exact TEXT or None")
    source_id = None if match.group(1) == "-" else match.group(1)
    return _OwnerScanCursor(
        source_changed=_ReasonScanCursor(
            last_conversation_id=source_id,
            cycle_dirty=match.group(2) == "1",
            wrap_next=match.group(3) == "1",
            resume_inclusive=match.group(4) == "1",
        ),
        backfill_pending=_ReasonScanCursor(
            last_conversation_id=(None if match.group(5) == "-" else match.group(5)),
            cycle_dirty=match.group(6) == "1",
            wrap_next=match.group(7) == "1",
            resume_inclusive=match.group(8) == "1",
        ),
        next_reason="source_changed" if match.group(9) == "s" else "backfill_pending",
    )


def _encode_cursor(cursor: _OwnerScanCursor) -> str:
    if type(cursor) is not _OwnerScanCursor or cursor.next_reason not in _RETRYABLE_REASONS:
        raise ValueError("conversation passage scan cursor is invalid")
    for lane in (cursor.source_changed, cursor.backfill_pending):
        if type(lane) is not _ReasonScanCursor or (
            lane.last_conversation_id is not None
            and _CONVERSATION_ID.fullmatch(lane.last_conversation_id) is None
        ):
            raise ValueError("conversation passage scan cursor is invalid")
    source = cursor.source_changed
    backfill = cursor.backfill_pending
    return (
        f"cpw2:{source.last_conversation_id or '-'}:{int(source.cycle_dirty)}:"
        f"{int(source.wrap_next)}:{int(source.resume_inclusive)}:"
        f"{backfill.last_conversation_id or '-'}:{int(backfill.cycle_dirty)}:"
        f"{int(backfill.wrap_next)}:{int(backfill.resume_inclusive)}:"
        f"{'s' if cursor.next_reason == 'source_changed' else 'b'}"
    )


def _digest(value: object) -> str | None:
    return value if type(value) is str and _SHA256.fullmatch(value) is not None else None


def _bounded_reason_conversations(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    reason: str,
    cursor: _ReasonScanCursor,
) -> _ReasonScanPage:
    """Scan one owner-keyed retry-reason lane without touching foreign owners."""

    if reason not in _RETRYABLE_REASONS:
        raise ValueError("conversation passage retry reason is invalid")
    scan_limit = CONVERSATION_PASSAGE_MAX_ACTIVE_CONVERSATIONS
    wrapped = cursor.wrap_next or cursor.last_conversation_id is None
    base_sql = """SELECT conversation.id
                    FROM conversations conversation
                         INDEXED BY idx_conversation_passage_conversation_owner_keyset
                   WHERE conversation.user_id=?"""
    if wrapped:
        rows = conn.execute(
            base_sql + " ORDER BY conversation.id ASC LIMIT ?",
            (principal_id, scan_limit + 1),
        ).fetchall()
    elif cursor.resume_inclusive:
        rows = conn.execute(
            base_sql + " AND conversation.id>=? ORDER BY conversation.id ASC LIMIT ?",
            (
                principal_id,
                cursor.last_conversation_id,
                scan_limit + 1,
            ),
        ).fetchall()
    else:
        rows = conn.execute(
            base_sql + " AND conversation.id>? ORDER BY conversation.id ASC LIMIT ?",
            (
                principal_id,
                cursor.last_conversation_id,
                scan_limit + 1,
            ),
        ).fetchall()

    raw_ids = tuple(str(row[0]) for row in rows[:scan_limit])
    if any(_CONVERSATION_ID.fullmatch(item) is None for item in raw_ids):
        raise sqlite3.DatabaseError("conversation passage reason scan identity is invalid")
    if not raw_ids:
        return _ReasonScanPage(
            reason,
            (),
            (),
            False if wrapped else cursor.cycle_dirty,
            False,
        )
    placeholders = ",".join("?" for _item in raw_ids)
    projection_rows = conn.execute(
        f"""SELECT projection.conversation_id,
                   projection.projection_status,
                   projection.incomplete_reason,
                   projection.passage_index_revision
              FROM conversation_passage_projections projection
             WHERE projection.conversation_id IN ({placeholders})
             ORDER BY projection.conversation_id ASC""",  # nosec B608 - placeholders only
        raw_ids,
    ).fetchall()
    projection_ids = tuple(row[0] for row in projection_rows)
    if projection_ids != raw_ids or any(
        type(row[0]) is not str
        or row[3] != CONVERSATION_PASSAGE_INDEX_REVISION
        or row[1] not in {"current", "incomplete"}
        or (
            (row[1] == "current" and row[2] is not None)
            or (row[1] == "incomplete" and row[2] not in {*_RETRYABLE_REASONS, "source_unavailable"})
        )
        for row in projection_rows
    ):
        raise sqlite3.DatabaseError("conversation passage reason scan projection is invalid")
    owned_ids = tuple(str(row[0]) for row in projection_rows if row[1] == "incomplete" and row[2] == reason)
    return _ReasonScanPage(
        reason,
        raw_ids,
        owned_ids,
        False if wrapped else cursor.cycle_dirty,
        len(rows) > scan_limit,
    )


def _projection_row(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    conversation_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT projection.*
             FROM conversation_passage_projections projection
             JOIN conversations conversation
               ON conversation.id=projection.conversation_id
             JOIN users principal
               ON principal.id=conversation.user_id AND principal.status='active'
            WHERE projection.conversation_id=? AND conversation.user_id=?""",
        (conversation_id, principal_id),
    ).fetchone()


def _validated_parent_prefix(
    conn: sqlite3.Connection,
    parent: sqlite3.Row,
) -> tuple[int, str | None, str | None, str]:
    conversation_id = str(parent["conversation_id"])
    passage_count = parent["passage_count"]
    if (
        type(passage_count) is not int
        or passage_count < 0
        or parent["indexed_message_count"] != passage_count
        or parent["passage_index_revision"] != CONVERSATION_PASSAGE_INDEX_REVISION
        or parent["projection_status"] != "incomplete"
        or parent["incomplete_reason"] not in _RETRYABLE_REASONS
    ):
        raise sqlite3.DatabaseError("conversation passage projection admission is inconsistent")

    child = conn.execute(
        """SELECT
                  (SELECT anchor_message_id
                     FROM conversation_passages
                    WHERE conversation_id=? AND anchor_ordinal=?
                    LIMIT 1) AS tail_id,
                  (SELECT conversation_prefix_sha256
                     FROM conversation_passages
                    WHERE conversation_id=? AND anchor_ordinal=?
                    LIMIT 1) AS tail_prefix,
                  EXISTS(
                      SELECT 1 FROM conversation_passages
                       WHERE conversation_id=? AND anchor_ordinal>=?
                       LIMIT 1
                  ) AS unexpected_suffix""",
        (
            conversation_id,
            passage_count - 1,
            conversation_id,
            passage_count - 1,
            conversation_id,
            passage_count,
        ),
    ).fetchone()
    if child is None or child["unexpected_suffix"] != 0:
        raise sqlite3.DatabaseError("conversation passage projection prefix is inconsistent")

    if passage_count == 0:
        if (
            child["tail_id"] is not None
            or child["tail_prefix"] is not None
            or parent["indexed_through_message_id"] is not None
        ):
            raise sqlite3.DatabaseError("conversation passage projection prefix is inconsistent")
        prefix = CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256
        empty_passage_set = CONVERSATION_PASSAGE_EMPTY_SET_SHA256
        stored_pair = (
            parent["indexed_conversation_revision_sha256"],
            parent["passage_set_sha256"],
        )
        allowed_pairs = (
            {(None, None), (prefix, empty_passage_set)}
            if parent["incomplete_reason"] == "source_changed"
            else {(None, None)}
        )
        if stored_pair not in allowed_pairs:
            raise sqlite3.DatabaseError("conversation passage projection prefix is inconsistent")
        # The stored empty proof is the seed, while the first extension helper
        # deliberately accepts ``None`` to prove that ordinal zero has no tail.
        return passage_count, None, None, empty_passage_set

    tail_id = child["tail_id"]
    tail_prefix = _digest(child["tail_prefix"])
    passage_set = _digest(parent["passage_set_sha256"])
    if (
        type(tail_id) is not str
        or _MESSAGE_ID.fullmatch(tail_id) is None
        or tail_id != parent["indexed_through_message_id"]
        or tail_prefix is None
        or tail_prefix != parent["indexed_conversation_revision_sha256"]
        or passage_set is None
    ):
        raise sqlite3.DatabaseError("conversation passage projection prefix is inconsistent")
    return passage_count, tail_id, tail_prefix, passage_set


def _next_source_descriptor(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    conversation_id: str,
    tail_message_id: str | None,
) -> sqlite3.Row | None:
    if tail_message_id is None:
        return conn.execute(
            """SELECT source.rowid AS source_rowid,source.id,
                      length(CAST(source.content AS BLOB)) AS content_bytes,
                      friday_conversation_passage_source_descriptor_valid(
                          source.id,source.conversation_id,?,source.user_id,?,
                          source.role,typeof(source.content),source.created_at
                      ) AS source_descriptor_valid
                 FROM messages source
                      INDEXED BY idx_conversation_passage_message_source_order
                WHERE source.user_id=? AND source.conversation_id=?
                  AND source.role IN ('user','assistant')
                ORDER BY source.rowid ASC LIMIT 1""",
            (conversation_id, principal_id, principal_id, conversation_id),
        ).fetchone()

    tail = conn.execute(
        """SELECT source.rowid AS source_rowid
             FROM messages source
            WHERE source.id=? AND source.conversation_id=? AND source.user_id=?
              AND source.role IN ('user','assistant')""",
        (tail_message_id, conversation_id, principal_id),
    ).fetchone()
    if tail is None or type(tail["source_rowid"]) is not int or tail["source_rowid"] < 1:
        raise sqlite3.DatabaseError("conversation passage tail source is unavailable")
    return conn.execute(
        """SELECT source.rowid AS source_rowid,source.id,
                      length(CAST(source.content AS BLOB)) AS content_bytes,
                      friday_conversation_passage_source_descriptor_valid(
                          source.id,source.conversation_id,?,source.user_id,?,
                          source.role,typeof(source.content),source.created_at
                      ) AS source_descriptor_valid
                 FROM messages source
                      INDEXED BY idx_conversation_passage_message_source_order
                WHERE source.user_id=? AND source.conversation_id=?
                  AND source.role IN ('user','assistant')
                  AND source.rowid>?
                ORDER BY source.rowid ASC LIMIT 1""",
        (
            conversation_id,
            principal_id,
            principal_id,
            conversation_id,
            tail["source_rowid"],
        ),
    ).fetchone()


def _source_row(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    conversation_id: str,
    source_rowid: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT id,conversation_id,user_id,role,
                  CAST(content AS BLOB) AS content_blob,
                  typeof(content) AS content_storage_class,
                  created_at,rowid AS source_rowid
             FROM messages
            WHERE rowid=? AND conversation_id=? AND user_id=?
              AND role IN ('user','assistant')""",
        (source_rowid, conversation_id, principal_id),
    ).fetchone()


def _unavailable_or_deferred(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    passage_count: int,
    source_bytes: int,
) -> _PublishStep:
    del conn, conversation_id, passage_count
    # Only the descriptor-proven first source whose byte length permanently
    # exceeds the fixed schema budget may enter ``source_unavailable``. A raced
    # or malformed body remains retryable; absence is never inferred from a bad
    # read or failed digest calculation.
    return _PublishStep("deferred", source_bytes)


def _publish_one_step(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    conversation_id: str,
    remaining_bytes: int,
) -> _PublishStep:
    """Advance one durable ordinal; return action and examined body bytes."""

    parent = _projection_row(
        conn,
        principal_id=principal_id,
        conversation_id=conversation_id,
    )
    if parent is None or parent["projection_status"] != "incomplete":
        return _PublishStep("unchanged")
    passage_count, tail_id, prefix, passage_set = _validated_parent_prefix(conn, parent)
    descriptor = _next_source_descriptor(
        conn,
        principal_id=principal_id,
        conversation_id=conversation_id,
        tail_message_id=tail_id,
    )
    if descriptor is None:
        completed_prefix = prefix or CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256
        changed = conn.execute(
            """UPDATE conversation_passage_projections
                  SET indexed_message_count=?,indexed_through_message_id=?,
                      indexed_conversation_revision_sha256=?,passage_set_sha256=?,
                      projection_status='current',incomplete_reason=NULL,
                      passage_count=?,projected_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                WHERE conversation_id=? AND projection_status='incomplete'
                  AND incomplete_reason IN ('backfill_pending','source_changed')
                  AND passage_count=?""",
            (
                passage_count,
                tail_id,
                completed_prefix,
                passage_set,
                passage_count,
                conversation_id,
                passage_count,
            ),
        )
        if changed.rowcount != 1:
            raise sqlite3.DatabaseError("conversation passage completion admission changed")
        return _PublishStep("current")

    source_bytes = descriptor["content_bytes"]
    if type(source_bytes) is not int or source_bytes < 0:
        raise sqlite3.DatabaseError("conversation passage source size is invalid")
    descriptor_valid = descriptor["source_descriptor_valid"]
    if type(descriptor_valid) is not int or descriptor_valid not in {0, 1}:
        raise sqlite3.DatabaseError("conversation passage source descriptor proof is invalid")
    if source_bytes > CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES:
        # Valid first-source oversize is settled during authenticated admission
        # or migration. Anything reaching the writer is a retained late tail
        # or corrupted/bypassed input and remains retryable without loading an
        # unaccounted body.
        return _PublishStep("deferred")
    if source_bytes > remaining_bytes:
        return _PublishStep("budget")
    source_rowid = descriptor["source_rowid"]
    if type(source_rowid) is not int or source_rowid < 1:
        raise sqlite3.DatabaseError("conversation passage source identity is invalid")
    source = _source_row(
        conn,
        principal_id=principal_id,
        conversation_id=conversation_id,
        source_rowid=source_rowid,
    )
    if source is None:
        raise sqlite3.DatabaseError("conversation passage source changed during publication")

    message_id = source["id"]
    content_blob = source["content_blob"]
    created_at = source["created_at"]
    if (
        _CONVERSATION_ID.fullmatch(conversation_id) is None
        or type(message_id) is not str
        or _MESSAGE_ID.fullmatch(message_id) is None
        or descriptor["id"] != message_id
        or source["source_rowid"] != source_rowid
        or source["conversation_id"] != conversation_id
        or source["user_id"] != principal_id
        or source["role"] not in {"user", "assistant"}
        or source["content_storage_class"] != "text"
        or type(content_blob) is not bytes
        or type(created_at) is not str
    ):
        return _unavailable_or_deferred(
            conn,
            conversation_id=conversation_id,
            passage_count=passage_count,
            source_bytes=source_bytes,
        )

    try:
        content = content_blob.decode("utf-8", errors="strict")
    except UnicodeError:
        content = None
    if content is None or len(content_blob) != source_bytes:
        return _unavailable_or_deferred(
            conn,
            conversation_id=conversation_id,
            passage_count=passage_count,
            source_bytes=source_bytes,
        )

    try:
        revision = conversation_passage_message_revision_sha256(
            message_id=message_id,
            conversation_id=conversation_id,
            principal_id=principal_id,
            role=str(source["role"]),
            content=content,
            created_at=created_at,
        )
        content_digest = conversation_passage_content_sha256(content)
        locator = conversation_passage_anchor_locator_sha256(
            conversation_id=conversation_id,
            anchor_message_id=message_id,
            anchor_ordinal=passage_count,
        )
        next_prefix = conversation_passage_prefix_sha256(prefix, passage_count, revision)
        next_set = conversation_passage_set_extend_sha256(
            passage_set,
            (
                passage_count,
                message_id,
                revision,
                content_digest,
                locator,
                next_prefix,
            ),
        )
    except (TypeError, UnicodeError, ValueError):
        return _unavailable_or_deferred(
            conn,
            conversation_id=conversation_id,
            passage_count=passage_count,
            source_bytes=source_bytes,
        )

    conn.execute(
        """INSERT INTO conversation_passages(
               conversation_id,anchor_message_id,anchor_ordinal,
               anchor_message_revision_sha256,anchor_content_sha256,
               anchor_locator_sha256,conversation_prefix_sha256
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            conversation_id,
            message_id,
            passage_count,
            revision,
            content_digest,
            locator,
            next_prefix,
        ),
    )
    # Schema 50's authenticated AFTER INSERT trigger performs the one matching
    # parent CAS in the same SQLite statement. A child-only commit is therefore
    # impossible; reselect only the body-free parent proof for the report.
    published = _projection_row(
        conn,
        principal_id=principal_id,
        conversation_id=conversation_id,
    )
    if (
        published is None
        or published["passage_count"] != passage_count + 1
        or published["indexed_message_count"] != passage_count + 1
        or published["indexed_through_message_id"] != message_id
        or published["indexed_conversation_revision_sha256"] != next_prefix
        or published["passage_set_sha256"] != next_set
        or published["projection_status"] not in {"current", "incomplete"}
        or (published["projection_status"] == "current" and published["incomplete_reason"] is not None)
        or (
            published["projection_status"] == "incomplete"
            and published["incomplete_reason"] != parent["incomplete_reason"]
        )
    ):
        raise sqlite3.DatabaseError("conversation passage append admission changed")
    final = published["projection_status"] == "current"
    return _PublishStep(
        "current" if final else "appended",
        source_bytes,
        anchor_written=True,
    )


def _backfill_conversation_passages_in_transaction(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    resume_at_conversation_id: str | None,
    limit: int = CONVERSATION_PASSAGE_DEFAULT_WORK_ITEMS,
) -> dict[str, Any]:
    """Advance one fair bounded owner batch inside the caller's transaction."""

    if type(conn) is not sqlite3.Connection or not conn.in_transaction:
        raise RuntimeError("conversation passage backfill requires a caller-owned transaction")
    principal = validate_user_id(principal_id)
    cursor = _bounded_cursor(resume_at_conversation_id)
    bounded = _bounded_limit(limit)
    validate_conversation_passage_schema(
        conn,
        validate_data=False,
        require_fts=False,
        validate_fts_data=False,
    )
    active_principal = conn.execute(
        "SELECT 1 FROM users WHERE id=? AND status='active' LIMIT 1",
        (principal,),
    ).fetchone()
    pages = (
        {
            "source_changed": _bounded_reason_conversations(
                conn,
                principal_id=principal,
                reason="source_changed",
                cursor=cursor.source_changed,
            ),
            "backfill_pending": _bounded_reason_conversations(
                conn,
                principal_id=principal,
                reason="backfill_pending",
                cursor=cursor.backfill_pending,
            ),
        }
        if active_principal is not None
        else {
            reason: _ReasonScanPage(reason, (), (), False, False)
            for reason in ("source_changed", "backfill_pending")
        }
    )
    queues = {reason: list(page.owned_ids) for reason, page in pages.items()}
    selected: list[tuple[str, str]] = []
    next_reason = cursor.next_reason
    selection_limit = min(bounded, CONVERSATION_PASSAGE_MAX_ACTIVE_CONVERSATIONS)
    while len(selected) < selection_limit and any(queues.values()):
        alternate = "backfill_pending" if next_reason == "source_changed" else "source_changed"
        chosen_reason = next_reason if queues[next_reason] else alternate
        if not queues[chosen_reason]:
            break
        selected.append((queues[chosen_reason].pop(0), chosen_reason))
        next_reason = "backfill_pending" if chosen_reason == "source_changed" else "source_changed"

    examined = 0
    anchors_written = 0
    current = 0
    unavailable = 0
    consumed_bytes = 0
    budget_reached = False
    unresolved = {
        reason: {
            conversation_id for conversation_id, selected_reason in selected if selected_reason == reason
        }
        for reason in ("source_changed", "backfill_pending")
    }
    selected_reason = {conversation_id: reason for conversation_id, reason in selected}

    active = [conversation_id for conversation_id, _reason in selected]
    while active and examined < bounded:
        next_round: list[str] = []
        for conversation_id in active:
            if examined >= bounded:
                next_round.append(conversation_id)
                continue
            step = _publish_one_step(
                conn,
                principal_id=principal,
                conversation_id=conversation_id,
                remaining_bytes=max(
                    0,
                    CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES - consumed_bytes,
                ),
            )
            action = step.action
            if action == "budget":
                # The descriptor was examined without loading its body. Count
                # this fixed work item and continue so a small sibling later in
                # the same keyset page cannot be starved by cumulative bytes.
                budget_reached = True
                examined += 1
                continue
            if action == "unchanged":
                unresolved[selected_reason[conversation_id]].discard(conversation_id)
                continue
            examined += 1
            consumed_bytes += step.source_bytes
            anchors_written += int(step.anchor_written)
            current += int(action == "current")
            unavailable += int(action == "unavailable")
            if action == "appended":
                next_round.append(conversation_id)
            elif action in {"current", "unavailable"}:
                unresolved[selected_reason[conversation_id]].discard(conversation_id)
        if not next_round:
            break
        active = next_round

    selected_by_reason = {
        reason: tuple(conversation_id for conversation_id, item_reason in selected if item_reason == reason)
        for reason in ("source_changed", "backfill_pending")
    }

    def advance_reason(reason: str, original: _ReasonScanCursor) -> tuple[_ReasonScanCursor, bool]:
        page = pages[reason]
        chosen = selected_by_reason[reason]
        cycle_dirty = page.cycle_dirty or bool(unresolved[reason])
        if len(chosen) < len(page.owned_ids):
            if not chosen:
                return original, True
            return (
                _ReasonScanCursor(
                    last_conversation_id=chosen[-1],
                    cycle_dirty=cycle_dirty,
                ),
                True,
            )
        last_id = page.raw_ids[-1] if page.raw_ids else original.last_conversation_id
        if page.has_raw_tail:
            return _ReasonScanCursor(last_id, cycle_dirty), True
        if cycle_dirty:
            return _ReasonScanCursor(last_id, True, True), True
        return _ReasonScanCursor(last_id), False

    source_cursor, source_has_more = advance_reason("source_changed", cursor.source_changed)
    backfill_cursor, backfill_has_more = advance_reason(
        "backfill_pending",
        cursor.backfill_pending,
    )
    has_more = source_has_more or backfill_has_more
    next_cursor = (
        _encode_cursor(
            _OwnerScanCursor(
                source_changed=source_cursor,
                backfill_pending=backfill_cursor,
                next_reason=next_reason,
            )
        )
        if has_more
        else None
    )
    return {
        "examined": examined,
        "anchors_written": anchors_written,
        "current": current,
        "explicit_incomplete": unavailable,
        "has_more": has_more,
        "next_resume_conversation_id": next_cursor,
        "message_bytes_examined": consumed_bytes,
        "message_byte_budget": CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES,
        "byte_budget_reached": (
            budget_reached or consumed_bytes == CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES
        ),
    }


def backfill_conversation_passages_in_transaction(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    resume_at_conversation_id: str | None,
    limit: int = CONVERSATION_PASSAGE_DEFAULT_WORK_ITEMS,
) -> dict[str, Any]:
    """Advance one batch atomically even when its outer transaction catches."""

    if type(conn) is not sqlite3.Connection or not conn.in_transaction:
        raise RuntimeError("conversation passage backfill requires a caller-owned transaction")
    conn.execute("SAVEPOINT friday_conversation_passage_backfill")
    try:
        report = _backfill_conversation_passages_in_transaction(
            conn,
            principal_id=principal_id,
            resume_at_conversation_id=resume_at_conversation_id,
            limit=limit,
        )
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT friday_conversation_passage_backfill")
        conn.execute("RELEASE SAVEPOINT friday_conversation_passage_backfill")
        raise
    conn.execute("RELEASE SAVEPOINT friday_conversation_passage_backfill")
    return report


__all__ = [
    "CONVERSATION_PASSAGE_DEFAULT_WORK_ITEMS",
    "CONVERSATION_PASSAGE_MAX_ACTIVE_CONVERSATIONS",
    "CONVERSATION_PASSAGE_MAX_WORK_ITEMS",
    "CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES",
    "backfill_conversation_passages_in_transaction",
]
