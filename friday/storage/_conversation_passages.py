"""Authenticated reader and bounded writer for conversation-passage anchors.

The caller owns one SQLite transaction and therefore the snapshot.  Authority
is established from ``users``, ``conversations`` and ``messages`` before this
module inspects any sidecar object.  Only then are the exact schema and accepted
prefix authenticated and a bounded page of anchor identities returned.

Message bodies are read only while deriving accepted-boundary/anchor digests or
feeding the derivative FTS index; neither the ordinary sidecar rows nor their
public read contracts contain a body.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

from friday.conversation_passages.contract import (
    CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256,
    CONVERSATION_PASSAGE_EMPTY_SET_SHA256,
    CONVERSATION_PASSAGE_INDEX_REVISION,
    CONVERSATION_PASSAGE_MAX_COUNT,
    CONVERSATION_PASSAGE_MAX_PAGE,
    ConversationPassageAnchor,
    ConversationPassageContractError,
    ConversationPassageProjectionRead,
)
from friday.conversation_passages.schema import (
    validate_conversation_passage_schema,
)
from friday.conversation_passages.worker_state import (
    CONVERSATION_PASSAGE_WORKER_STATE_KEY,
    ConversationPassageWorkerState,
    conversation_passage_owner_scan_key,
    decode_conversation_passage_scan_cursor,
    decode_conversation_passage_worker_state,
    encode_conversation_passage_scan_cursor,
    encode_conversation_passage_worker_state,
    load_conversation_passage_worker_namespace_key,
    next_conversation_passage_generation,
)
from friday.conversation_passages.writer import (
    CONVERSATION_PASSAGE_DEFAULT_WORK_ITEMS,
    backfill_conversation_passages_in_transaction,
)
from friday.storage._base import (
    StorageShared,
    deleted_account_tombstone_key,
    utc_now,
)
from friday.user_ids import USER_ID_RE

_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}\Z")
_MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{16}\Z")
_OWNER_SCAN_PAGE = 32


class ConversationPassageStorageError(ValueError):
    """A body-free closed failure at the conversation-passage read boundary."""


def _principal_id(value: object) -> str:
    if type(value) is not str or USER_ID_RE.fullmatch(value) is None:
        raise ConversationPassageStorageError("principal identity is invalid")
    return value


def _conversation_id(value: object, *, label: str) -> str:
    if type(value) is not str or _CONVERSATION_ID_RE.fullmatch(value) is None:
        raise ConversationPassageStorageError(f"{label} is invalid")
    return value


def _message_id(value: object, *, label: str) -> str:
    if type(value) is not str or _MESSAGE_ID_RE.fullmatch(value) is None:
        raise ConversationPassageStorageError(f"{label} is invalid")
    return value


def _bounded_integer(value: object, *, label: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ConversationPassageStorageError(f"{label} is invalid")
    return value


def _stored_integer(value: object, *, label: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ConversationPassageStorageError(f"stored {label} is invalid")
    return value


def _stored_text(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise ConversationPassageStorageError(f"stored {label} is invalid")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ConversationPassageStorageError(f"stored {label} is invalid") from None
    return value


def _canonical_boundary_utc(value: object) -> str:
    """Keep byte compatibility with the released archive boundary identity."""

    if type(value) is not str or not value or value != value.strip() or len(value) > 64:
        raise ConversationPassageStorageError("stored accepted boundary is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ConversationPassageStorageError("stored accepted boundary is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ConversationPassageStorageError("stored accepted boundary is invalid")
    normalized = parsed.astimezone(UTC).isoformat()
    if normalized != value:
        raise ConversationPassageStorageError("stored accepted boundary is invalid")
    return normalized


def _accepted_boundary_identity(values: dict[str, Any]) -> str:
    """Derive the released exact-window boundary digest without retaining a body."""

    content = _stored_text(values["boundary_content"], label="accepted boundary")
    payload = {
        "schema": "friday.private-message-window-boundary.v1",
        "id": _message_id(values["boundary_id"], label="stored accepted boundary"),
        "conversation_id": _conversation_id(
            values["boundary_conversation_id"],
            label="stored accepted boundary conversation",
        ),
        "person_id": _principal_id(values["boundary_principal_id"]),
        "role": "user",
        "content": content,
        "created_at": _canonical_boundary_utc(values["boundary_created_at"]),
    }
    material = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(material).hexdigest()


def _one_record(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    columns = tuple(str(item[0]) for item in (cursor.description or ()))
    raw = cursor.fetchone()
    cursor.close()
    if raw is None:
        return None
    return dict(zip(columns, tuple(raw), strict=True))


def _records(cursor: sqlite3.Cursor) -> tuple[dict[str, Any], ...]:
    columns = tuple(str(item[0]) for item in (cursor.description or ()))
    rows = tuple(dict(zip(columns, tuple(raw), strict=True)) for raw in cursor.fetchall())
    cursor.close()
    return rows


def _authorize_before_sidecar(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    boundary_conversation_id: str,
    boundary_user_message_id: str,
    conversation_id: str,
) -> tuple[int, str] | None:
    """Authenticate the accepted turn and target using authoritative tables only."""

    cursor = conn.execute(
        """WITH active_principal AS MATERIALIZED (
                   SELECT principal.id
                     FROM users principal
                    WHERE principal.id=? AND principal.status='active'
               ),
               owned_boundary_conversation AS MATERIALIZED (
                   SELECT conversation.id,conversation.user_id
                     FROM conversations conversation
                     JOIN active_principal principal
                       ON principal.id=conversation.user_id
                    WHERE conversation.id=?
               ),
               accepted_boundary AS MATERIALIZED (
                   SELECT boundary.rowid AS boundary_rowid,
                          boundary.id AS boundary_id,
                          boundary.conversation_id AS boundary_conversation_id,
                          boundary.user_id AS boundary_principal_id,
                          boundary.role AS boundary_role,
                          boundary.content AS boundary_content,
                          boundary.created_at AS boundary_created_at
                     FROM messages boundary
                     JOIN owned_boundary_conversation conversation
                       ON conversation.id=boundary.conversation_id
                      AND conversation.user_id=boundary.user_id
                    WHERE boundary.id=?
                      AND boundary.conversation_id=?
                      AND boundary.user_id=?
                      AND boundary.role='user'
               ),
               owned_target_conversation AS MATERIALIZED (
                   SELECT conversation.id,conversation.user_id
                     FROM conversations conversation
                     JOIN active_principal principal
                       ON principal.id=conversation.user_id
                    WHERE conversation.id=?
               )
               SELECT boundary.*,
                      target.id AS target_conversation_id,
                      target.user_id AS target_principal_id
                 FROM accepted_boundary boundary
                 CROSS JOIN owned_target_conversation target""",
        (
            principal_id,
            boundary_conversation_id,
            boundary_user_message_id,
            boundary_conversation_id,
            principal_id,
            conversation_id,
        ),
    )
    values = _one_record(cursor)
    if values is None:
        return None
    boundary_rowid = _stored_integer(
        values["boundary_rowid"],
        label="accepted boundary row identity",
        low=1,
        high=9_223_372_036_854_775_807,
    )
    if (
        values["boundary_id"] != boundary_user_message_id
        or values["boundary_conversation_id"] != boundary_conversation_id
        or values["boundary_principal_id"] != principal_id
        or values["boundary_role"] != "user"
        or values["target_conversation_id"] != conversation_id
        or values["target_principal_id"] != principal_id
    ):
        raise ConversationPassageStorageError("stored conversation authority is invalid")
    return boundary_rowid, _accepted_boundary_identity(values)


def _projection_parent(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    conversation_id: str,
    boundary_rowid: int,
) -> dict[str, Any]:
    """Authenticate only the immutable source/anchor prefix at the boundary.

    Proof cost is linear in this target conversation's accepted prefix and its
    matching children, irrespective of page size.  The scoped message index
    keeps unrelated tenants and conversations outside that unavoidable work.
    """

    cursor = conn.execute(
        """WITH owned_target AS MATERIALIZED (
                   SELECT conversation.id,conversation.user_id
                     FROM conversations conversation
                     JOIN users principal
                       ON principal.id=conversation.user_id
                      AND principal.status='active'
                    WHERE conversation.id=? AND conversation.user_id=?
               ),
               target_source AS MATERIALIZED (
                   SELECT source.rowid AS source_rowid,
                          source.id AS source_id,
                          source.conversation_id AS source_conversation_id,
                          source.user_id AS source_principal_id,
                          source.created_at AS source_created_at
                     FROM messages source INDEXED BY idx_messages_conversation
                     JOIN owned_target target
                      ON target.id=source.conversation_id
                      AND target.user_id=source.user_id
                    WHERE source.user_id=?
                      AND source.conversation_id=?
                      AND source.role IN ('user','assistant')
               ),
               authorized_source AS MATERIALIZED (
                   SELECT *
                     FROM target_source
                    WHERE source_rowid<?
               ),
               authorized_source_order AS MATERIALIZED (
                   SELECT source.*,
                          ROW_NUMBER() OVER (
                              ORDER BY source.source_rowid ASC
                          )-1 AS source_ordinal
                     FROM authorized_source source
               ),
               mapped_children AS MATERIALIZED (
                   SELECT passage.*,
                          source.source_rowid,
                          source.source_id,
                          source.source_conversation_id,
                          source.source_principal_id,
                          source.source_created_at,
                          source.source_ordinal
                     FROM authorized_source_order source
                     JOIN conversation_passages passage
                       ON passage.conversation_id=
                              (SELECT id FROM owned_target)
                      AND passage.anchor_message_id=source.source_id
               ),
               target_children AS MATERIALIZED (
                   SELECT passage.*,
                          previous.passage_rowid AS previous_rowid,
                          previous.conversation_prefix_sha256 AS previous_prefix
                     FROM mapped_children passage
                     LEFT JOIN mapped_children previous
                       ON previous.conversation_id=passage.conversation_id
                      AND previous.anchor_ordinal=passage.anchor_ordinal-1
               ),
               source_rollup AS MATERIALIZED (
                   SELECT COALESCE(SUM(source_rowid<?),0)
                              AS authorized_message_count,
                          COALESCE(SUM(source_rowid>=?),0)
                              AS future_message_count
                     FROM target_source
               ),
               all_child_rollup AS MATERIALIZED (
                   SELECT COUNT(*) AS child_count
                     FROM conversation_passages passage
                     JOIN owned_target target
                       ON target.id=passage.conversation_id
               ),
               child_rollup AS MATERIALIZED (
                   SELECT COUNT(*) AS authorized_projected_count,
                          COALESCE(SUM(
                              source_ordinal<>anchor_ordinal
                              OR conversation_prefix_sha256<>
                                 friday_conversation_passage_prefix_sha256(
                                     CASE WHEN anchor_ordinal=0
                                          THEN NULL ELSE previous_prefix END,
                                     anchor_ordinal,
                                     anchor_message_revision_sha256)
                              OR (anchor_ordinal=0 AND previous_rowid IS NOT NULL)
                              OR (anchor_ordinal>0 AND previous_rowid IS NULL)
                          ),0) AS invalid_child_count,
                          friday_conversation_passage_set_sha256(
                              anchor_ordinal,anchor_message_id,
                              anchor_message_revision_sha256,
                              anchor_content_sha256,
                              anchor_locator_sha256,
                              conversation_prefix_sha256
                          ) AS calculated_set_sha256
                     FROM (
                         SELECT *
                           FROM target_children
                          ORDER BY anchor_ordinal ASC
                          LIMIT -1
                     ) ordered_children
               ),
               authorized_tail AS MATERIALIZED (
                   SELECT anchor_message_id,conversation_prefix_sha256
                     FROM target_children
                    ORDER BY anchor_ordinal DESC LIMIT 1
               )
               SELECT projection.conversation_id,
                      projection.passage_index_revision,
                      source_rollup.authorized_message_count,
                      child_rollup.authorized_projected_count,
                      (SELECT anchor_message_id FROM authorized_tail)
                          AS authorized_indexed_through_message_id,
                      COALESCE(
                          (SELECT conversation_prefix_sha256 FROM authorized_tail),
                          ?
                      ) AS authorized_conversation_revision_sha256,
                      COALESCE(child_rollup.calculated_set_sha256,?)
                          AS authorized_passage_set_sha256
                 FROM conversation_passage_projections projection
                 JOIN owned_target target
                   ON target.id=projection.conversation_id
                 CROSS JOIN source_rollup
                 CROSS JOIN all_child_rollup
                 CROSS JOIN child_rollup
                WHERE projection.passage_index_revision=?
                  AND child_rollup.invalid_child_count=0
                  AND child_rollup.authorized_projected_count<=
                      source_rollup.authorized_message_count
                  AND (
                      source_rollup.future_message_count>0
                      OR (
                          friday_conversation_passage_projection_valid(
                              projection.conversation_id,
                              projection.indexed_message_count,
                              projection.indexed_through_message_id,
                              projection.indexed_conversation_revision_sha256,
                              projection.passage_set_sha256,
                              projection.passage_index_revision,
                              projection.projection_status,
                              projection.incomplete_reason,
                              projection.passage_count)=1
                          AND projection.passage_count=
                              child_rollup.authorized_projected_count
                          AND all_child_rollup.child_count=
                              child_rollup.authorized_projected_count
                          AND (
                              projection.passage_count=0
                              OR (
                                  projection.indexed_through_message_id=
                                      (SELECT anchor_message_id FROM authorized_tail)
                                  AND projection.indexed_conversation_revision_sha256=
                                      (SELECT conversation_prefix_sha256 FROM authorized_tail)
                                  AND projection.passage_set_sha256=
                                      child_rollup.calculated_set_sha256
                              )
                          )
                          AND (
                              projection.projection_status<>'current'
                              OR projection.passage_count=
                                  source_rollup.authorized_message_count
                          )
                      )
                  )""",
        (
            conversation_id,
            principal_id,
            principal_id,
            conversation_id,
            boundary_rowid,
            boundary_rowid,
            boundary_rowid,
            CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256,
            CONVERSATION_PASSAGE_EMPTY_SET_SHA256,
            CONVERSATION_PASSAGE_INDEX_REVISION,
        ),
    )
    values = _one_record(cursor)
    if values is None:
        raise ConversationPassageStorageError("conversation passage projection authority is unavailable")
    return values


def _projection_anchors(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    conversation_id: str,
    boundary_rowid: int,
    anchor_offset: int,
    limit: int,
    expected_child_count: int,
) -> tuple[ConversationPassageAnchor, ...]:
    """Validate mapped bodies once, returning only a bounded body-free page."""

    cursor = conn.execute(
        """WITH owned_target AS MATERIALIZED (
                   SELECT conversation.id,conversation.user_id
                     FROM conversations conversation
                     JOIN users principal
                       ON principal.id=conversation.user_id
                      AND principal.status='active'
                    WHERE conversation.id=? AND conversation.user_id=?
               ),
               authorized_source AS MATERIALIZED (
                   SELECT source.rowid AS source_rowid,
                          source.id AS source_id,
                          source.conversation_id AS source_conversation_id,
                          source.user_id AS source_principal_id,
                          source.created_at AS source_created_at
                     FROM messages source INDEXED BY idx_messages_conversation
                     JOIN owned_target target
                       ON target.id=source.conversation_id
                      AND target.user_id=source.user_id
                    WHERE source.user_id=?
                      AND source.conversation_id=?
                      AND source.role IN ('user','assistant')
                      AND source.rowid<?
               ),
               authorized_source_order AS MATERIALIZED (
                   SELECT source.*,
                          ROW_NUMBER() OVER (
                              ORDER BY source.source_rowid ASC
                          )-1 AS source_ordinal
                     FROM authorized_source source
               ),
               mapped_children AS MATERIALIZED (
                   SELECT passage.conversation_id,
                          passage.anchor_message_id,
                          passage.anchor_ordinal,
                          passage.anchor_message_revision_sha256,
                          passage.anchor_content_sha256,
                          passage.anchor_locator_sha256,
                          passage.conversation_prefix_sha256,
                          source.source_rowid,
                          source.source_id,
                          source.source_conversation_id,
                          source.source_principal_id,
                          source.source_created_at,
                          source.source_ordinal
                     FROM authorized_source_order source
                     JOIN owned_target target
                       ON target.id=source.source_conversation_id
                      AND target.user_id=source.source_principal_id
                     JOIN conversation_passages passage
                       ON passage.conversation_id=target.id
                      AND passage.anchor_message_id=source.source_id
               ),
               validated_children AS MATERIALIZED (
                   SELECT mapped.conversation_id,
                          mapped.anchor_message_id,
                          mapped.anchor_ordinal,
                          mapped.anchor_message_revision_sha256,
                          mapped.anchor_content_sha256,
                          mapped.anchor_locator_sha256,
                          mapped.conversation_prefix_sha256,
                          mapped.source_rowid AS rowid,
                          CASE WHEN mapped.source_ordinal<>mapped.anchor_ordinal
                                 OR friday_conversation_passage_anchor_valid(
                                        source.id,source.conversation_id,
                                        source.user_id,target.user_id,
                                        source.role,source.content,source.created_at,
                                        mapped.conversation_id,
                                        mapped.anchor_message_id,
                                        mapped.anchor_ordinal,
                                        mapped.anchor_message_revision_sha256,
                                        mapped.anchor_content_sha256,
                                        mapped.anchor_locator_sha256)<>1
                               THEN 1 ELSE 0 END AS digest_invalid
                     FROM mapped_children mapped
                     JOIN owned_target target
                       ON target.id=mapped.source_conversation_id
                      AND target.user_id=mapped.source_principal_id
                     JOIN messages source
                       ON source.rowid=mapped.source_rowid
                      AND source.id=mapped.source_id
                      AND source.conversation_id=target.id
                      AND source.user_id=target.user_id
                      AND source.role IN ('user','assistant')
                      AND source.created_at=mapped.source_created_at
               ),
               proof AS MATERIALIZED (
                   SELECT COUNT(*) AS child_count,
                          COALESCE(SUM(digest_invalid),0) AS invalid_child_count
                     FROM validated_children
               ),
               page AS MATERIALIZED (
                   SELECT conversation_id,anchor_message_id,
                          anchor_ordinal,anchor_message_revision_sha256,
                          anchor_content_sha256,anchor_locator_sha256,
                          conversation_prefix_sha256
                     FROM validated_children
                    ORDER BY anchor_ordinal ASC, rowid ASC
                    LIMIT ? OFFSET ?
               )
               SELECT proof.child_count,proof.invalid_child_count,page.*
                 FROM proof
                 LEFT JOIN page ON 1=1
                WHERE proof.child_count=? AND proof.invalid_child_count=0
                ORDER BY page.anchor_ordinal ASC""",
        (
            conversation_id,
            principal_id,
            principal_id,
            conversation_id,
            boundary_rowid,
            limit,
            anchor_offset,
            expected_child_count,
        ),
    )
    records = _records(cursor)
    if not records:
        raise ConversationPassageStorageError("conversation passage mapped body proof is invalid")
    if any(
        values["child_count"] != expected_child_count or values["invalid_child_count"] != 0
        for values in records
    ):
        raise ConversationPassageStorageError("conversation passage mapped body proof is invalid")
    return tuple(
        ConversationPassageAnchor(
            conversation_id=values["conversation_id"],
            anchor_message_id=values["anchor_message_id"],
            anchor_ordinal=values["anchor_ordinal"],
            anchor_message_revision_sha256=values["anchor_message_revision_sha256"],
            anchor_content_sha256=values["anchor_content_sha256"],
            anchor_locator_sha256=values["anchor_locator_sha256"],
            conversation_prefix_sha256=values["conversation_prefix_sha256"],
        )
        for values in records
        if values["anchor_message_id"] is not None
    )


def _select_authorized_conversation_passage_projection_in_transaction(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    boundary_conversation_id: str,
    origin_boundary_user_message_id: str,
    conversation_id: str,
    anchor_offset: int,
    limit: int,
) -> ConversationPassageProjectionRead | None:
    principal = _principal_id(principal_id)
    boundary_conversation = _conversation_id(
        boundary_conversation_id,
        label="accepted boundary conversation identity",
    )
    boundary_message = _message_id(
        origin_boundary_user_message_id,
        label="accepted boundary message identity",
    )
    source_conversation = _conversation_id(
        conversation_id,
        label="source conversation identity",
    )
    offset = _bounded_integer(
        anchor_offset,
        label="conversation passage anchor offset",
        low=0,
        high=CONVERSATION_PASSAGE_MAX_COUNT,
    )
    page_limit = _bounded_integer(
        limit,
        label="conversation passage page limit",
        low=1,
        high=CONVERSATION_PASSAGE_MAX_PAGE,
    )

    # This query and boundary digest intentionally precede every sqlite_master,
    # sidecar parent, child, view or FTS access.
    authority = _authorize_before_sidecar(
        conn,
        principal_id=principal,
        boundary_conversation_id=boundary_conversation,
        boundary_user_message_id=boundary_message,
        conversation_id=source_conversation,
    )
    if authority is None:
        return None
    boundary_rowid, boundary_identity_sha256 = authority

    validate_conversation_passage_schema(
        conn,
        required=True,
        validate_data=False,
        require_fts=False,
        validate_fts_data=False,
    )
    parent = _projection_parent(
        conn,
        principal_id=principal,
        conversation_id=source_conversation,
        boundary_rowid=boundary_rowid,
    )
    authorized_message_count = _stored_integer(
        parent["authorized_message_count"],
        label="authorized message count",
        low=0,
        high=CONVERSATION_PASSAGE_MAX_COUNT,
    )
    authorized_projected_count = _stored_integer(
        parent["authorized_projected_count"],
        label="authorized projected count",
        low=0,
        high=CONVERSATION_PASSAGE_MAX_COUNT,
    )
    if authorized_projected_count > authorized_message_count:
        raise ConversationPassageStorageError("conversation passage authorized coverage is invalid")
    if offset > authorized_projected_count:
        raise ConversationPassageStorageError("conversation passage anchor offset is outside the projection")

    anchors = (
        ()
        if authorized_projected_count == 0
        else _projection_anchors(
            conn,
            principal_id=principal,
            conversation_id=source_conversation,
            boundary_rowid=boundary_rowid,
            anchor_offset=offset,
            limit=page_limit,
            expected_child_count=authorized_projected_count,
        )
    )
    expected_page_count = min(page_limit, authorized_projected_count - offset)
    if len(anchors) != expected_page_count:
        raise ConversationPassageStorageError("conversation passage authorized page is incomplete")

    return ConversationPassageProjectionRead(
        conversation_id=parent["conversation_id"],
        passage_index_revision=parent["passage_index_revision"],
        boundary_identity_sha256=boundary_identity_sha256,
        authorized_message_count=authorized_message_count,
        authorized_projected_count=authorized_projected_count,
        authorized_projection_complete=(authorized_projected_count == authorized_message_count),
        authorized_indexed_through_message_id=parent["authorized_indexed_through_message_id"],
        authorized_conversation_revision_sha256=parent["authorized_conversation_revision_sha256"],
        authorized_passage_set_sha256=parent["authorized_passage_set_sha256"],
        anchor_offset=offset,
        anchors=anchors,
        has_more=offset + len(anchors) < authorized_projected_count,
    )


def select_authorized_conversation_passage_projection_in_transaction(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    boundary_conversation_id: str,
    origin_boundary_user_message_id: str,
    conversation_id: str,
    anchor_offset: int = 0,
    limit: int = CONVERSATION_PASSAGE_MAX_PAGE,
) -> ConversationPassageProjectionRead | None:
    """Read one authenticated pre-boundary page from the dormant sidecar.

    ``None`` is reserved for a missing, inactive or foreign accepted boundary,
    or an unowned target conversation.  A malformed call or any authenticated
    schema/data inconsistency fails through one body-free closed error.
    """

    if type(conn) is not sqlite3.Connection or not conn.in_transaction:
        raise RuntimeError("conversation passage reader requires a caller-owned transaction")
    try:
        return _select_authorized_conversation_passage_projection_in_transaction(
            conn,
            principal_id=principal_id,
            boundary_conversation_id=boundary_conversation_id,
            origin_boundary_user_message_id=origin_boundary_user_message_id,
            conversation_id=conversation_id,
            anchor_offset=anchor_offset,
            limit=limit,
        )
    except ConversationPassageStorageError:
        raise
    except (
        ConversationPassageContractError,
        LookupError,
        OverflowError,
        TypeError,
        UnicodeError,
        ValueError,
        sqlite3.Error,
    ):
        raise ConversationPassageStorageError("conversation passage projection is unavailable") from None


class ConversationPassagesMixin(StorageShared):
    def backfill_conversation_passages(
        self,
        user_id: str,
        *,
        resume_at_conversation_id: str | None = None,
        limit: int = CONVERSATION_PASSAGE_DEFAULT_WORK_ITEMS,
    ) -> dict[str, Any]:
        """Advance one bounded owner page; the parent count is its durable cursor."""

        with self.transaction() as conn:
            return backfill_conversation_passages_in_transaction(
                conn,
                principal_id=user_id,
                resume_at_conversation_id=resume_at_conversation_id,
                limit=limit,
            )

    def run_conversation_passage_worker_tick(
        self,
        *,
        expected_value: str | None,
        limit: int = CONVERSATION_PASSAGE_DEFAULT_WORK_ITEMS,
    ) -> dict[str, Any]:
        """Keyset-select, CAS-admit and execute one owner in one transaction.

        The identity-free numeric owner cursor is advanced before passage work.
        A competing manager with the same runtime snapshot loses without work.
        Selection reads at most two fixed raw ``users.rowid`` pages and never
        materializes or status-filters the owner corpus.
        """

        if expected_value is not None and type(expected_value) is not str:
            raise ValueError("expected conversation passage worker state must be TEXT or None")
        state, supported = decode_conversation_passage_worker_state(expected_value)
        if not supported:
            raise ValueError("conversation passage worker state is unsupported")

        with self.transaction() as conn:
            row = conn.execute(
                "SELECT value FROM runtime_kv WHERE key=?",
                (CONVERSATION_PASSAGE_WORKER_STATE_KEY,),
            ).fetchone()
            current_value = str(row["value"]) if row is not None else None
            if current_value != expected_value:
                return {"admitted": False, "report": None, "phase_error": None}

            namespace_key = load_conversation_passage_worker_namespace_key(conn)
            if state.owner_cursor is None:
                owner_rows = conn.execute(
                    """SELECT rowid,id,status FROM users
                        ORDER BY rowid ASC LIMIT ?""",
                    (_OWNER_SCAN_PAGE + 1,),
                ).fetchall()
            else:
                owner_rows = conn.execute(
                    """SELECT rowid,id,status FROM users
                        WHERE rowid>? ORDER BY rowid ASC LIMIT ?""",
                    (state.owner_cursor, _OWNER_SCAN_PAGE + 1),
                ).fetchall()
                if not owner_rows:
                    owner_rows = conn.execute(
                        """SELECT rowid,id,status FROM users
                            ORDER BY rowid ASC LIMIT ?""",
                        (_OWNER_SCAN_PAGE + 1,),
                    ).fetchall()
            if not owner_rows:
                return {"admitted": True, "report": None, "phase_error": None}

            bounded_owner_rows = owner_rows[:_OWNER_SCAN_PAGE]
            if not bounded_owner_rows:
                raise sqlite3.DatabaseError("conversation passage owner keyset page is invalid")
            owner: str | None = None
            owner_rowid: int | None = None
            last_examined_rowid: int | None = None
            for candidate in bounded_owner_rows:
                candidate_rowid = candidate["rowid"]
                if type(candidate_rowid) is not int:
                    raise sqlite3.DatabaseError("conversation passage owner rowid is invalid")
                last_examined_rowid = candidate_rowid
                if candidate["status"] != "active":
                    continue
                try:
                    candidate_owner = _principal_id(candidate["id"])
                except ConversationPassageStorageError:
                    # A legacy unsupported active identity consumes only its
                    # numeric keyset position. It grants no writer authority and
                    # cannot pin valid owners later in this fixed raw page.
                    continue
                if conn.execute(
                    "SELECT 1 FROM runtime_kv WHERE key=? LIMIT 1",
                    (deleted_account_tombstone_key(candidate_owner),),
                ).fetchone():
                    continue
                owner = candidate_owner
                owner_rowid = candidate_rowid
                break
            next_owner_cursor = owner_rowid if owner_rowid is not None else last_examined_rowid
            if type(next_owner_cursor) is not int:
                raise sqlite3.DatabaseError("conversation passage owner keyset did not advance")
            next_state = ConversationPassageWorkerState(
                owner_cursor=next_owner_cursor,
                generation=next_conversation_passage_generation(state.generation),
            )
            next_value = encode_conversation_passage_worker_state(next_state)
            claimed = (
                conn.execute(
                    "INSERT OR IGNORE INTO runtime_kv(key,value,updated_at) VALUES(?,?,?)",
                    (CONVERSATION_PASSAGE_WORKER_STATE_KEY, next_value, utc_now()),
                )
                if expected_value is None
                else conn.execute(
                    "UPDATE runtime_kv SET value=?,updated_at=? WHERE key=? AND value=?",
                    (next_value, utc_now(), CONVERSATION_PASSAGE_WORKER_STATE_KEY, expected_value),
                )
            )
            if claimed.rowcount != 1:
                return {"admitted": False, "report": None, "phase_error": None}
            if owner is None:
                return {"admitted": True, "report": None, "phase_error": None}

            report: dict[str, Any] | None = None
            phase_error: str | None = None
            conn.execute("SAVEPOINT friday_conversation_passage_worker_phase")
            try:
                owner_scan_key = conversation_passage_owner_scan_key(
                    owner,
                    namespace_key=namespace_key,
                )
                cursor_row = conn.execute(
                    "SELECT value FROM runtime_kv WHERE key=?",
                    (owner_scan_key,),
                ).fetchone()
                owner_scan_cursor = (
                    decode_conversation_passage_scan_cursor(cursor_row["value"])
                    if cursor_row is not None
                    else None
                )
                report = backfill_conversation_passages_in_transaction(
                    conn,
                    principal_id=owner,
                    resume_at_conversation_id=owner_scan_cursor,
                    limit=limit,
                )
                has_more = report.get("has_more")
                next_cursor = report.get("next_resume_conversation_id")
                if has_more is True and type(next_cursor) is str and next_cursor:
                    conn.execute(
                        """INSERT INTO runtime_kv(key,value,updated_at) VALUES(?,?,?)
                           ON CONFLICT(key) DO UPDATE SET
                               value=excluded.value,updated_at=excluded.updated_at""",
                        (
                            owner_scan_key,
                            encode_conversation_passage_scan_cursor(next_cursor),
                            utc_now(),
                        ),
                    )
                elif has_more is False and next_cursor is None:
                    conn.execute("DELETE FROM runtime_kv WHERE key=?", (owner_scan_key,))
                else:
                    raise ValueError("conversation passage writer cursor result is invalid")
            except Exception as exc:  # rotate after the writer savepoint is physically rolled back
                conn.execute("ROLLBACK TO SAVEPOINT friday_conversation_passage_worker_phase")
                conn.execute("RELEASE SAVEPOINT friday_conversation_passage_worker_phase")
                report = None
                phase_error = type(exc).__name__
            else:
                conn.execute("RELEASE SAVEPOINT friday_conversation_passage_worker_phase")
            return {"admitted": True, "report": report, "phase_error": phase_error}


__all__ = [
    "ConversationPassagesMixin",
    "ConversationPassageStorageError",
    "select_authorized_conversation_passage_projection_in_transaction",
]
