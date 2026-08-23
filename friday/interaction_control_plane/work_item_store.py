"""Transaction-local store for the first durable RecallConversation Work Item.

Every mutation requires an already-open caller transaction.  This lets the
runtime commit the Work Item revision beside the owned assistant row and its
accepted capability receipt, rather than leaving recoverable-looking state for
an answer that never became durable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from friday.interaction_control_plane.work_item_contract import (
    WORK_ITEM_MAX_REVISION,
    WORK_ITEM_TTL_HOURS,
    RecallConversationActiveFrame,
    RecallConversationWorkItem,
    RecallMessageRole,
    WorkCompletionContract,
    WorkGoal,
    WorkItemContractError,
    WorkKind,
    WorkPlaybook,
    WorkState,
    WorkTransition,
    canonical_work_item_instant,
)
from friday.orchestration.capability_outcome import (
    CapabilityOutcomeError,
    CapabilityOutcomeStatus,
    load_accepted_capability_outcome_receipt,
)
from friday.orchestration.contracts import RouteClass
from friday.orchestration.message_window_outcome import (
    LegacyMessageWindowPlan,
    MessageWindowOutcomeError,
)

_WORK_ITEM_ID_RE = re.compile(r"work_[0-9a-f]{16}")
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}")
_MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{16}")
_USER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_ACCEPTED_MESSAGE_WINDOW_PLAN_KEY = "accepted_message_window_plan"
_SOURCE_BEARING_OUTCOMES = frozenset(
    {
        CapabilityOutcomeStatus.COMPLETE,
        CapabilityOutcomeStatus.PARTIAL,
        CapabilityOutcomeStatus.EMPTY,
    }
)


class WorkItemConflictError(RuntimeError):
    """The expected current Work Item revision/state is no longer current."""


class WorkItemAnchorError(ValueError):
    """An owned message/outcome anchor is absent, stale or not exact."""


def new_recall_conversation_work_item_id() -> str:
    """Precompute an opaque ID for trace HMAC binding before publication."""

    return f"work_{uuid.uuid4().hex[:16]}"


def _require_transaction(conn: sqlite3.Connection) -> None:
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("work item store requires a sqlite3 connection")
    if not conn.in_transaction:
        raise RuntimeError("work item store requires an existing transaction")


def _scope(value: object, pattern: re.Pattern[str], *, label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise WorkItemContractError(f"{label} is not a valid identifier")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise WorkItemContractError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _rowid(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise WorkItemAnchorError(f"{label} is not a valid SQLite row identity")
    return value


def _now(value: str | None) -> str:
    candidate = value or datetime.now(UTC).isoformat(timespec="seconds")
    return canonical_work_item_instant(candidate, label="now")


def _expiry(now: str) -> str:
    parsed = datetime.fromisoformat(now)
    return (parsed + timedelta(hours=WORK_ITEM_TTL_HOURS)).isoformat(timespec="seconds")


def _logical_now(requested: str | None, *, current_updated_at: str | None = None) -> str:
    """Keep lifecycle time monotonic across wall-clock corrections."""

    timestamp = _now(requested)
    if current_updated_at is None:
        return timestamp
    current = canonical_work_item_instant(current_updated_at, label="current_updated_at")
    if current != current_updated_at:
        raise WorkItemContractError("stored Work Item timestamp is not canonical")
    return max(timestamp, current)


def _row_mapping(cursor: sqlite3.Cursor, row: object) -> dict[str, object]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if isinstance(row, Mapping):
        return {str(key): value for key, value in row.items()}
    if isinstance(row, tuple) and cursor.description is not None:
        return {str(column[0]): value for column, value in zip(cursor.description, row, strict=True)}
    raise WorkItemContractError("work item query returned an invalid row")


def _fetch_work_item(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
) -> RecallConversationWorkItem | None:
    cursor = conn.execute(
        """SELECT * FROM work_items
            WHERE id=? AND user_id=? AND conversation_id=?""",
        (work_item_id, user_id, conversation_id),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return RecallConversationWorkItem.from_storage_row(_row_mapping(cursor, row))


def _decode_anchor_metadata(value: object) -> tuple[Mapping[str, object], Any]:
    if not isinstance(value, str):
        raise WorkItemAnchorError("assistant anchor metadata is invalid")
    try:
        receipt = load_accepted_capability_outcome_receipt(value)
        decoded = json.loads(value)
    except (CapabilityOutcomeError, json.JSONDecodeError, UnicodeError, TypeError) as exc:
        raise WorkItemAnchorError("assistant anchor has no valid accepted outcome") from exc
    if not isinstance(decoded, Mapping):
        raise WorkItemAnchorError("assistant anchor metadata is invalid")
    return decoded, receipt


def _owned_text_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise WorkItemAnchorError(f"{label} is not text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise WorkItemAnchorError(f"{label} is not valid UTF-8") from exc
    if len(encoded) > 100_000:
        raise WorkItemAnchorError(f"{label} exceeds the closed plan limit")
    return hashlib.sha256(encoded).hexdigest()


def _validate_accepted_plan_carrier(
    structural: Mapping[str, object],
    *,
    boundary_content: object,
    user_id: str,
    conversation_id: str,
    boundary_user_message_id: str,
    active_frame: RecallConversationActiveFrame,
    accepted_plan_sha256: str,
) -> None:
    """Rebuild the digest-only plan around its one opaque tenant digest."""

    raw_plan = structural.get(_ACCEPTED_MESSAGE_WINDOW_PLAN_KEY)
    if not isinstance(raw_plan, Mapping):
        raise WorkItemAnchorError("assistant anchor has no accepted message-window plan")
    try:
        tenant_sha256 = _digest(raw_plan.get("tenant_sha256"), label="tenant_sha256")
        reconstructed = LegacyMessageWindowPlan(
            request_sha256=_owned_text_sha256(boundary_content, label="boundary content"),
            tenant_sha256=tenant_sha256,
            person_sha256=_owned_text_sha256(user_id, label="user_id"),
            conversation_sha256=_owned_text_sha256(conversation_id, label="conversation_id"),
            timezone_sha256=_owned_text_sha256(
                active_frame.timezone_name,
                label="timezone_name",
            ),
            since_utc_sha256=_owned_text_sha256(active_frame.since_utc, label="since_utc"),
            until_utc_sha256=_owned_text_sha256(active_frame.until_utc, label="until_utc"),
            boundary_message_sha256=_owned_text_sha256(
                boundary_user_message_id,
                label="boundary_user_message_id",
            ),
            role=active_frame.role.selector_value,
        )
    except (MessageWindowOutcomeError, WorkItemContractError) as exc:
        raise WorkItemAnchorError("assistant anchor plan carrier is invalid") from exc
    if dict(raw_plan) != reconstructed.payload() or not hmac.compare_digest(
        reconstructed.canonical_sha256(),
        accepted_plan_sha256,
    ):
        raise WorkItemAnchorError("assistant anchor plan does not match its owned active frame")


def _validate_accepted_anchor(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    boundary_user_message_id: str,
    assistant_message_id: str,
    accepted_plan_sha256: str,
    accepted_outcome_sha256: str,
    require_latest_message: bool,
    expected_active_frame: RecallConversationActiveFrame,
    allow_disabled_owner: bool = False,
) -> None:
    """Re-read one exact anchor and hash, but never retain, its boundary body."""

    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    boundary = _scope(
        boundary_user_message_id,
        _MESSAGE_ID_RE,
        label="anchor_user_message_id",
    )
    assistant = _scope(
        assistant_message_id,
        _MESSAGE_ID_RE,
        label="anchor_assistant_message_id",
    )
    plan_digest = _digest(accepted_plan_sha256, label="accepted_plan_sha256")
    outcome_digest = _digest(accepted_outcome_sha256, label="accepted_outcome_sha256")

    cursor = conn.execute(
        """SELECT assistant.metadata_json AS assistant_metadata_json,
                  boundary.content AS boundary_content,
                  boundary.rowid AS boundary_rowid,
                  assistant.rowid AS assistant_rowid
             FROM users owner
             JOIN conversations conversation
               ON conversation.user_id=owner.id AND conversation.id=?
             JOIN messages boundary
               ON boundary.id=? AND boundary.user_id=owner.id
              AND boundary.conversation_id=conversation.id AND boundary.role='user'
             JOIN messages assistant
              ON assistant.id=? AND assistant.user_id=owner.id
              AND assistant.conversation_id=conversation.id AND assistant.role='assistant'
              AND assistant.reply_to=boundary.id
              AND boundary.rowid<assistant.rowid
              AND NOT EXISTS (
                  SELECT 1 FROM messages intervening
                   WHERE intervening.user_id=owner.id
                     AND intervening.conversation_id=conversation.id
                     AND intervening.rowid>boundary.rowid
                     AND intervening.rowid<assistant.rowid
              )
            WHERE owner.id=? AND owner.status IN ('active','disabled')
              AND (? OR owner.status='active')
            LIMIT 1""",
        (conversation, boundary, assistant, user, int(allow_disabled_owner)),
    )
    row = cursor.fetchone()
    if row is None:
        raise WorkItemAnchorError("work item publication anchor is not owned and exact")
    anchor = _row_mapping(cursor, row)
    if require_latest_message:
        later = conn.execute(
            """SELECT 1 FROM messages
                WHERE user_id=? AND conversation_id=? AND rowid>?
                LIMIT 1""",
            (user, conversation, _rowid(anchor["assistant_rowid"], label="assistant_rowid")),
        ).fetchone()
        if later is not None:
            raise WorkItemAnchorError("assistant anchor is not the latest owned message")

    metadata, receipt = _decode_anchor_metadata(anchor["assistant_metadata_json"])
    outcome = receipt.outcome
    if (
        outcome.route is not RouteClass.ORDINARY_DIALOGUE
        or outcome.status not in _SOURCE_BEARING_OUTCOMES
        or outcome.plan_sha256 != plan_digest
        or receipt.outcome_sha256 != outcome_digest
    ):
        raise WorkItemAnchorError("assistant anchor outcome does not match RecallConversation")
    structural = metadata.get("structural")
    if not isinstance(structural, Mapping) or (
        structural.get("verdict_kind") != "message_window"
        or structural.get("answer_present") is not True
        or structural.get("model_spoke") is not False
        or structural.get("message_window_status") != outcome.status.value
    ):
        raise WorkItemAnchorError("assistant anchor is not an owned exact message-window verdict")
    _validate_accepted_plan_carrier(
        structural,
        boundary_content=anchor["boundary_content"],
        user_id=user,
        conversation_id=conversation,
        boundary_user_message_id=boundary,
        active_frame=expected_active_frame,
        accepted_plan_sha256=plan_digest,
    )


def _validate_immediate_previous_assistant(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    previous_assistant_message_id: str,
    boundary_user_message_id: str,
    require_boundary_latest: bool,
) -> None:
    """Prove that no same-conversation message intervenes before a follow-up."""

    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    previous = _scope(
        previous_assistant_message_id,
        _MESSAGE_ID_RE,
        label="previous_assistant_message_id",
    )
    boundary = _scope(boundary_user_message_id, _MESSAGE_ID_RE, label="boundary_user_message_id")
    cursor = conn.execute(
        """SELECT previous.rowid AS previous_rowid, boundary.rowid AS boundary_rowid
             FROM messages previous
             JOIN messages boundary
               ON boundary.user_id=previous.user_id
              AND boundary.conversation_id=previous.conversation_id
            WHERE previous.id=? AND previous.user_id=? AND previous.conversation_id=?
              AND previous.role='assistant'
              AND boundary.id=? AND boundary.role='user'
              AND previous.rowid<boundary.rowid
              AND NOT EXISTS (
                  SELECT 1 FROM messages intervening
                   WHERE intervening.user_id=previous.user_id
                     AND intervening.conversation_id=previous.conversation_id
                     AND intervening.rowid>previous.rowid
                     AND intervening.rowid<boundary.rowid
              )
            LIMIT 1""",
        (previous, user, conversation, boundary),
    )
    row = cursor.fetchone()
    if row is None:
        raise WorkItemAnchorError("follow-up does not immediately follow the anchored assistant")
    if require_boundary_latest:
        values = _row_mapping(cursor, row)
        later = conn.execute(
            """SELECT 1 FROM messages
                WHERE user_id=? AND conversation_id=? AND rowid>?
                LIMIT 1""",
            (user, conversation, _rowid(values["boundary_rowid"], label="boundary_rowid")),
        ).fetchone()
        if later is not None:
            raise WorkItemAnchorError("follow-up boundary is no longer the latest owned message")


def create_recall_conversation_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    timezone_name: str,
    since_utc: str,
    until_utc: str,
    role: RecallMessageRole,
    anchor_user_message_id: str,
    anchor_assistant_message_id: str,
    accepted_plan_sha256: str,
    accepted_outcome_sha256: str,
    now: str | None = None,
    work_item_id: str | None = None,
) -> RecallConversationWorkItem:
    """Create active work beside its first accepted exact-window publication."""

    _require_transaction(conn)
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    identifier = work_item_id or new_recall_conversation_work_item_id()
    _scope(identifier, _WORK_ITEM_ID_RE, label="work_item_id")
    if not isinstance(role, RecallMessageRole):
        raise WorkItemContractError("role must be a RecallMessageRole")
    frame = RecallConversationActiveFrame.create(
        timezone_name=timezone_name,
        since_utc=since_utc,
        until_utc=until_utc,
        role=role,
    )
    plan_digest = _digest(accepted_plan_sha256, label="accepted_plan_sha256")
    outcome_digest = _digest(accepted_outcome_sha256, label="accepted_outcome_sha256")
    _validate_accepted_anchor(
        conn,
        user_id=user,
        conversation_id=conversation,
        boundary_user_message_id=anchor_user_message_id,
        assistant_message_id=anchor_assistant_message_id,
        accepted_plan_sha256=plan_digest,
        accepted_outcome_sha256=outcome_digest,
        require_latest_message=True,
        expected_active_frame=frame,
    )
    timestamp = _now(now)
    active_cursor = conn.execute(
        """SELECT id,revision,updated_at,expires_at FROM work_items
            WHERE user_id=? AND conversation_id=? AND state='active'
            LIMIT 1""",
        (user, conversation),
    )
    active_row = active_cursor.fetchone()
    if active_row is not None:
        active = _row_mapping(active_cursor, active_row)
        active_revision = active["revision"]
        if not isinstance(active_revision, int) or isinstance(active_revision, bool):
            raise WorkItemContractError("stored Work Item revision is invalid")
        timestamp = _logical_now(now, current_updated_at=str(active["updated_at"] or ""))
        active_expiry = canonical_work_item_instant(active["expires_at"], label="expires_at")
        if active_expiry != active["expires_at"]:
            raise WorkItemContractError("stored Work Item expiry is not canonical")
        if active_revision >= WORK_ITEM_MAX_REVISION:
            # This bounded operational frame has no children or outcome ledger in
            # P2.  A saturated revision cannot be advanced honestly, so replace
            # it atomically with the new full request instead of breaking chat.
            retired = conn.execute(
                """DELETE FROM work_items
                    WHERE id=? AND user_id=? AND conversation_id=?
                      AND state='active' AND revision=?""",
                (active["id"], user, conversation, active_revision),
            )
        elif active_expiry <= timestamp:
            retired = conn.execute(
                """UPDATE work_items
                      SET state='expired',transition='expired',revision=revision+1,
                          updated_at=?,closed_at=?
                    WHERE id=? AND user_id=? AND conversation_id=?
                      AND state='active' AND revision=? AND expires_at<=?""",
                (
                    timestamp,
                    timestamp,
                    active["id"],
                    user,
                    conversation,
                    active_revision,
                    timestamp,
                ),
            )
        else:
            retired = conn.execute(
                """UPDATE work_items
                      SET state='suspended',transition='suspended',revision=revision+1,
                          updated_at=?,closed_at=NULL
                    WHERE id=? AND user_id=? AND conversation_id=?
                      AND state='active' AND revision=? AND expires_at>?""",
                (
                    timestamp,
                    active["id"],
                    user,
                    conversation,
                    active_revision,
                    timestamp,
                ),
            )
        if retired.rowcount != 1:
            raise WorkItemConflictError("active Work Item retirement lost its revision race")
    try:
        conn.execute(
            """INSERT INTO work_items(
                   id,user_id,conversation_id,kind,goal,state,playbook,
                   completion_contract,active_frame_json,anchor_user_message_id,
                   anchor_assistant_message_id,accepted_plan_sha256,
                   accepted_outcome_sha256,revision,transition,created_at,
                   updated_at,expires_at,closed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
            (
                identifier,
                user,
                conversation,
                WorkKind.RECALL_CONVERSATION.value,
                WorkGoal.EXACT_CURRENT_CONVERSATION_RECALL.value,
                WorkState.ACTIVE.value,
                WorkPlaybook.RECALL_CONVERSATION.value,
                WorkCompletionContract.ACCEPTED_EXACT_OWNED_MESSAGE_WINDOW.value,
                frame.to_json(),
                anchor_user_message_id,
                anchor_assistant_message_id,
                plan_digest,
                outcome_digest,
                1,
                WorkTransition.CREATED.value,
                timestamp,
                timestamp,
                _expiry(timestamp),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise WorkItemConflictError("Work Item creation lost its current-state race") from exc
    created = _fetch_work_item(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if created is None:  # pragma: no cover - same transaction inserted the row
        raise WorkItemConflictError("created Work Item is not durable in the caller transaction")
    _validate_accepted_anchor(
        conn,
        user_id=user,
        conversation_id=conversation,
        boundary_user_message_id=created.anchor_user_message_id,
        assistant_message_id=created.anchor_assistant_message_id,
        accepted_plan_sha256=created.accepted_plan_sha256,
        accepted_outcome_sha256=created.accepted_outcome_sha256,
        require_latest_message=True,
        expected_active_frame=created.active_frame,
    )
    return created


def get_recall_conversation_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
) -> RecallConversationWorkItem | None:
    """Load one owner-scoped item and revalidate its accepted outcome anchor."""

    _require_transaction(conn)
    item = _fetch_work_item(
        conn,
        work_item_id=_scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id"),
        user_id=_scope(user_id, _USER_ID_RE, label="user_id"),
        conversation_id=_scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id"),
    )
    if item is None:
        return None
    _validate_accepted_anchor(
        conn,
        user_id=item.user_id,
        conversation_id=item.conversation_id,
        boundary_user_message_id=item.anchor_user_message_id,
        assistant_message_id=item.anchor_assistant_message_id,
        accepted_plan_sha256=item.accepted_plan_sha256,
        accepted_outcome_sha256=item.accepted_outcome_sha256,
        require_latest_message=False,
        expected_active_frame=item.active_frame,
    )
    return item


def get_recall_conversation_work_item_for_export_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
) -> RecallConversationWorkItem | None:
    """Integrity-check one owner export while admitting a disabled owner."""

    _require_transaction(conn)
    item = _fetch_work_item(
        conn,
        work_item_id=_scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id"),
        user_id=_scope(user_id, _USER_ID_RE, label="user_id"),
        conversation_id=_scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id"),
    )
    if item is None:
        return None
    _validate_accepted_anchor(
        conn,
        user_id=item.user_id,
        conversation_id=item.conversation_id,
        boundary_user_message_id=item.anchor_user_message_id,
        assistant_message_id=item.anchor_assistant_message_id,
        accepted_plan_sha256=item.accepted_plan_sha256,
        accepted_outcome_sha256=item.accepted_outcome_sha256,
        require_latest_message=False,
        expected_active_frame=item.active_frame,
        allow_disabled_owner=True,
    )
    return item


def get_current_recall_conversation_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    boundary_user_message_id: str | None = None,
    now: str | None = None,
) -> RecallConversationWorkItem | None:
    """Load the one unexpired active item, optionally bound to an immediate follow-up."""

    _require_transaction(conn)
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    timestamp = _now(now)
    cursor = conn.execute(
        """SELECT * FROM work_items
            WHERE user_id=? AND conversation_id=? AND state='active' AND expires_at>?
            LIMIT 1""",
        (user, conversation, timestamp),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    item = RecallConversationWorkItem.from_storage_row(_row_mapping(cursor, row))
    _validate_accepted_anchor(
        conn,
        user_id=user,
        conversation_id=conversation,
        boundary_user_message_id=item.anchor_user_message_id,
        assistant_message_id=item.anchor_assistant_message_id,
        accepted_plan_sha256=item.accepted_plan_sha256,
        accepted_outcome_sha256=item.accepted_outcome_sha256,
        require_latest_message=boundary_user_message_id is None,
        expected_active_frame=item.active_frame,
    )
    if boundary_user_message_id is not None:
        _validate_immediate_previous_assistant(
            conn,
            user_id=user,
            conversation_id=conversation,
            previous_assistant_message_id=item.anchor_assistant_message_id,
            boundary_user_message_id=boundary_user_message_id,
            require_boundary_latest=True,
        )
    return item


def cas_update_recall_conversation_constraints_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    since_utc: str,
    until_utc: str,
    new_boundary_user_message_id: str,
    new_assistant_message_id: str,
    new_accepted_plan_sha256: str,
    new_accepted_outcome_sha256: str,
    now: str | None = None,
) -> RecallConversationWorkItem:
    """CAS-replace only time bounds and publication anchors on active work."""

    _require_transaction(conn)
    if (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or not 1 <= expected_revision < WORK_ITEM_MAX_REVISION
    ):
        raise WorkItemContractError("expected_revision is outside the closed limit")
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    identifier = _scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
    current = _fetch_work_item(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if current is None or current.state is not WorkState.ACTIVE or current.revision != expected_revision:
        raise WorkItemConflictError("Work Item revision/state is no longer current")
    timestamp = _logical_now(now, current_updated_at=current.updated_at)
    if current.expires_at <= timestamp:
        raise WorkItemConflictError("Work Item revision/state is no longer current")
    _validate_accepted_anchor(
        conn,
        user_id=user,
        conversation_id=conversation,
        boundary_user_message_id=current.anchor_user_message_id,
        assistant_message_id=current.anchor_assistant_message_id,
        accepted_plan_sha256=current.accepted_plan_sha256,
        accepted_outcome_sha256=current.accepted_outcome_sha256,
        require_latest_message=False,
        expected_active_frame=current.active_frame,
    )
    _validate_immediate_previous_assistant(
        conn,
        user_id=user,
        conversation_id=conversation,
        previous_assistant_message_id=current.anchor_assistant_message_id,
        boundary_user_message_id=new_boundary_user_message_id,
        require_boundary_latest=False,
    )
    plan_digest = _digest(new_accepted_plan_sha256, label="new_accepted_plan_sha256")
    outcome_digest = _digest(new_accepted_outcome_sha256, label="new_accepted_outcome_sha256")
    frame = current.active_frame.with_time_window(since_utc=since_utc, until_utc=until_utc)
    _validate_accepted_anchor(
        conn,
        user_id=user,
        conversation_id=conversation,
        boundary_user_message_id=new_boundary_user_message_id,
        assistant_message_id=new_assistant_message_id,
        accepted_plan_sha256=plan_digest,
        accepted_outcome_sha256=outcome_digest,
        require_latest_message=True,
        expected_active_frame=frame,
    )
    cursor = conn.execute(
        """UPDATE work_items
              SET active_frame_json=?,anchor_user_message_id=?,
                  anchor_assistant_message_id=?,accepted_plan_sha256=?,
                  accepted_outcome_sha256=?,revision=revision+1,
                  transition='constraint_updated',updated_at=?,expires_at=?
            WHERE id=? AND user_id=? AND conversation_id=?
              AND state='active' AND revision=? AND expires_at>?""",
        (
            frame.to_json(),
            new_boundary_user_message_id,
            new_assistant_message_id,
            plan_digest,
            outcome_digest,
            timestamp,
            _expiry(timestamp),
            identifier,
            user,
            conversation,
            expected_revision,
            timestamp,
        ),
    )
    if cursor.rowcount != 1:
        raise WorkItemConflictError("Work Item constraint CAS lost its revision race")
    updated = _fetch_work_item(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if updated is None:  # pragma: no cover - same transaction updated the row
        raise WorkItemConflictError("updated Work Item is not durable in the caller transaction")
    _validate_accepted_anchor(
        conn,
        user_id=user,
        conversation_id=conversation,
        boundary_user_message_id=updated.anchor_user_message_id,
        assistant_message_id=updated.anchor_assistant_message_id,
        accepted_plan_sha256=updated.accepted_plan_sha256,
        accepted_outcome_sha256=updated.accepted_outcome_sha256,
        require_latest_message=True,
        expected_active_frame=updated.active_frame,
    )
    return updated


def _cas_state_transition(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    from_states: frozenset[WorkState],
    target_state: WorkState,
    transition: WorkTransition,
    now: str | None,
    require_due: bool,
) -> RecallConversationWorkItem:
    _require_transaction(conn)
    if (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or not 1 <= expected_revision < WORK_ITEM_MAX_REVISION
    ):
        raise WorkItemContractError("expected_revision is outside the closed limit")
    identifier = _scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    current = _fetch_work_item(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if current is None or current.state not in from_states or current.revision != expected_revision:
        raise WorkItemConflictError("Work Item revision/state is no longer current")
    timestamp = _logical_now(now, current_updated_at=current.updated_at)
    due = current.expires_at <= timestamp
    if due is not require_due:
        raise WorkItemConflictError("Work Item revision/state is no longer current")
    closed_at = timestamp if target_state in {WorkState.CANCELLED, WorkState.EXPIRED} else None
    expiry_predicate = "expires_at<=?" if require_due else "expires_at>?"
    state_placeholders = ",".join("?" for _state in from_states)
    parameters: list[object] = [
        target_state.value,
        transition.value,
        timestamp,
        closed_at,
        identifier,
        user,
        conversation,
        expected_revision,
        *(state.value for state in sorted(from_states, key=lambda state: state.value)),
        timestamp,
    ]
    cursor = conn.execute(
        f"""UPDATE work_items
               SET state=?,transition=?,revision=revision+1,updated_at=?,closed_at=?
             WHERE id=? AND user_id=? AND conversation_id=? AND revision=?
               AND state IN ({state_placeholders}) AND {expiry_predicate}""",  # nosec B608
        parameters,
    )
    if cursor.rowcount != 1:
        raise WorkItemConflictError("Work Item state CAS lost its revision race")
    updated = _fetch_work_item(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if updated is None:  # pragma: no cover
        raise WorkItemConflictError("transitioned Work Item is not durable")
    return updated


def suspend_recall_conversation_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    now: str | None = None,
) -> RecallConversationWorkItem:
    return _cas_state_transition(
        conn,
        work_item_id=work_item_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
        from_states=frozenset({WorkState.ACTIVE}),
        target_state=WorkState.SUSPENDED,
        transition=WorkTransition.SUSPENDED,
        now=now,
        require_due=False,
    )


def cancel_recall_conversation_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    now: str | None = None,
) -> RecallConversationWorkItem:
    return _cas_state_transition(
        conn,
        work_item_id=work_item_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
        from_states=frozenset({WorkState.ACTIVE, WorkState.SUSPENDED}),
        target_state=WorkState.CANCELLED,
        transition=WorkTransition.CANCELLED,
        now=now,
        require_due=False,
    )


def expire_recall_conversation_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    now: str | None = None,
) -> RecallConversationWorkItem:
    return _cas_state_transition(
        conn,
        work_item_id=work_item_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
        from_states=frozenset({WorkState.ACTIVE, WorkState.SUSPENDED}),
        target_state=WorkState.EXPIRED,
        transition=WorkTransition.EXPIRED,
        now=now,
        require_due=True,
    )


def expire_due_recall_conversation_work_items_in_transaction(
    conn: sqlite3.Connection,
    *,
    now: str | None = None,
    user_id: str | None = None,
) -> int:
    """Expire bounded due rows, optionally under one exact owner scope."""

    _require_transaction(conn)
    timestamp = _now(now)
    if user_id is None:
        exhausted = conn.execute(
            """DELETE FROM work_items
                WHERE state IN ('active','suspended') AND expires_at<=?
                  AND revision>=2147483647""",
            (timestamp,),
        )
        cursor = conn.execute(
            """UPDATE work_items
                  SET state='expired',transition='expired',revision=revision+1,
                      updated_at=?,closed_at=?
                WHERE state IN ('active','suspended') AND expires_at<=?
                  AND revision<2147483647""",
            (timestamp, timestamp, timestamp),
        )
    else:
        user = _scope(user_id, _USER_ID_RE, label="user_id")
        exhausted = conn.execute(
            """DELETE FROM work_items
                WHERE user_id=? AND state IN ('active','suspended') AND expires_at<=?
                  AND revision>=2147483647""",
            (user, timestamp),
        )
        cursor = conn.execute(
            """UPDATE work_items
                  SET state='expired',transition='expired',revision=revision+1,
                      updated_at=?,closed_at=?
                WHERE user_id=? AND state IN ('active','suspended') AND expires_at<=?
                  AND revision<2147483647""",
            (timestamp, timestamp, user, timestamp),
        )
    return max(0, int(exhausted.rowcount or 0)) + max(0, int(cursor.rowcount or 0))


__all__ = [
    "WorkItemAnchorError",
    "WorkItemConflictError",
    "cancel_recall_conversation_work_item_in_transaction",
    "cas_update_recall_conversation_constraints_in_transaction",
    "create_recall_conversation_work_item_in_transaction",
    "expire_due_recall_conversation_work_items_in_transaction",
    "expire_recall_conversation_work_item_in_transaction",
    "get_current_recall_conversation_work_item_in_transaction",
    "get_recall_conversation_work_item_for_export_in_transaction",
    "get_recall_conversation_work_item_in_transaction",
    "new_recall_conversation_work_item_id",
    "suspend_recall_conversation_work_item_in_transaction",
]
