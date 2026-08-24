"""Transaction-local store for durable selected-archive-evidence recall.

The base Work Item and its immutable body-free evidence sidecar are created in
one caller-owned transaction.  Every later mutation revalidates both the
current publication anchor and the exact next user/assistant pair before a CAS
revision update.  No request, excerpt, title, filename or model prose is copied
into either Work Item table.
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

from friday.interaction_control_plane.archive_evidence_work_item import (
    RecallSelectedArchiveEvidenceActiveFrame,
    RecallSelectedArchiveEvidenceWorkItem,
)
from friday.interaction_control_plane.selected_archive_evidence import (
    SelectedArchiveEvidence,
)
from friday.interaction_control_plane.work_item_contract import (
    WORK_ITEM_MAX_REVISION,
    WORK_ITEM_TTL_HOURS,
    WorkCompletionContract,
    WorkGoal,
    WorkItemContractError,
    WorkKind,
    WorkPlaybook,
    WorkState,
    WorkTransition,
    canonical_work_item_instant,
)
from friday.interaction_control_plane.work_item_store import (
    WorkItemAnchorError,
    WorkItemConflictError,
)
from friday.orchestration.archive_recall_outcome import (
    ArchiveRecallLane,
    ArchiveRecallOutcomeError,
    ArchiveRecallStatus,
    load_accepted_archive_recall_outcome_receipt,
)
from friday.retrieval.archive_search_authority import ArchiveSearchSelectedEvidence
from friday.retrieval.archive_search_contract import ArchiveSearchCorpus

_WORK_ITEM_ID_RE = re.compile(r"work_[0-9a-f]{16}\Z")
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}\Z")
_MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{16}\Z")
_USER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_BEARING_STATUSES = frozenset({ArchiveRecallStatus.COMPLETE, ArchiveRecallStatus.PARTIAL})
_SOURCE_FREE_STATUSES = frozenset(
    {
        ArchiveRecallStatus.DENIED,
        ArchiveRecallStatus.DRIFTED,
        ArchiveRecallStatus.UNAVAILABLE,
    }
)


def new_recall_selected_archive_evidence_work_item_id() -> str:
    """Precompute the opaque identifier needed by the immutable sidecar."""

    return f"work_{uuid.uuid4().hex[:16]}"


def _require_transaction(conn: sqlite3.Connection) -> None:
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("archive Work Item store requires a sqlite3 connection")
    if not conn.in_transaction:
        raise RuntimeError("archive Work Item store requires an existing transaction")


def _scope(value: object, pattern: re.Pattern[str], *, label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise WorkItemContractError(f"{label} is not a valid identifier")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise WorkItemContractError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _revision(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value < WORK_ITEM_MAX_REVISION:
        raise WorkItemContractError("expected_revision is outside the closed limit")
    return value


def _now(value: str | None) -> str:
    candidate = value or datetime.now(UTC).isoformat(timespec="seconds")
    return canonical_work_item_instant(candidate, label="now")


def _expiry(now: str) -> str:
    parsed = datetime.fromisoformat(now)
    return (parsed + timedelta(hours=WORK_ITEM_TTL_HOURS)).isoformat(timespec="seconds")


def _logical_now(requested: str | None, *, current_updated_at: str | None = None) -> str:
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
    raise WorkItemContractError("archive Work Item query returned an invalid row")


def _fetch_archive_work_item(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
) -> RecallSelectedArchiveEvidenceWorkItem | None:
    work_cursor = conn.execute(
        """SELECT * FROM work_items
            WHERE id=? AND user_id=? AND conversation_id=?
              AND kind='recall_selected_archive_evidence'""",
        (work_item_id, user_id, conversation_id),
    )
    work_row = work_cursor.fetchone()
    if work_row is None:
        return None
    evidence_cursor = conn.execute(
        "SELECT * FROM work_item_selected_evidence WHERE work_item_id=?",
        (work_item_id,),
    )
    evidence_row = evidence_cursor.fetchone()
    if evidence_row is None:
        raise WorkItemContractError("archive Work Item has no selected-evidence sidecar")
    selected = SelectedArchiveEvidence.from_storage_row(_row_mapping(evidence_cursor, evidence_row))
    return RecallSelectedArchiveEvidenceWorkItem.from_storage_rows(
        _row_mapping(work_cursor, work_row),
        selected,
    )


def _archive_selection(evidence: SelectedArchiveEvidence) -> ArchiveSearchSelectedEvidence:
    try:
        return ArchiveSearchSelectedEvidence(
            corpus=ArchiveSearchCorpus(evidence.corpus.value),
            source_ref=evidence.source_ref,
            passage_refs=evidence.passage_refs,
            resolved_snapshot_sha256=evidence.source_snapshot_sha256,
        )
    except (TypeError, ValueError) as exc:
        raise WorkItemAnchorError("selected archive evidence is not replayable") from exc


def _text_sha256(value: object, *, label: str, maximum_bytes: int) -> str:
    if not isinstance(value, str):
        raise WorkItemAnchorError(f"{label} is not text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise WorkItemAnchorError(f"{label} is not valid UTF-8") from exc
    if not encoded or len(encoded) > maximum_bytes:
        raise WorkItemAnchorError(f"{label} exceeds the closed limit")
    return hashlib.sha256(encoded).hexdigest()


def _replay_plan_sha256(boundary_content: object, evidence: SelectedArchiveEvidence) -> str:
    request_sha256 = _text_sha256(
        boundary_content,
        label="archive replay request",
        maximum_bytes=256,
    )
    selected = _archive_selection(evidence)
    selected_sha256 = hashlib.sha256(selected.to_private_json().encode("ascii")).hexdigest()
    encoded = json.dumps(
        {
            "request_sha256": request_sha256,
            "schema": "friday.selected-archive-evidence-replay-plan.v1",
            "selected_evidence_sha256": selected_sha256,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _validate_archive_anchor(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    boundary_user_message_id: str,
    assistant_message_id: str,
    accepted_plan_sha256: str,
    accepted_outcome_sha256: str,
    evidence: SelectedArchiveEvidence,
    expected_lane: ArchiveRecallLane | None,
    expected_source_bearing: bool | None,
    require_latest_message: bool,
    allow_disabled_owner: bool = False,
) -> None:
    """Re-read an owned exact publication and bind its receipt to the sidecar."""

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
                  assistant.content AS assistant_content,
                  boundary.content AS boundary_content,
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
              AND assistant.reply_to=boundary.id AND boundary.rowid<assistant.rowid
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
        raise WorkItemAnchorError("archive publication anchor is not owned and exact")
    anchor = _row_mapping(cursor, row)
    if require_latest_message:
        assistant_rowid = anchor["assistant_rowid"]
        if not isinstance(assistant_rowid, int) or isinstance(assistant_rowid, bool) or assistant_rowid < 1:
            raise WorkItemAnchorError("archive assistant anchor row is invalid")
        if (
            conn.execute(
                """SELECT 1 FROM messages
                WHERE user_id=? AND conversation_id=? AND rowid>? LIMIT 1""",
                (user, conversation, assistant_rowid),
            ).fetchone()
            is not None
        ):
            raise WorkItemAnchorError("archive assistant anchor is not the latest publication")

    try:
        receipt = load_accepted_archive_recall_outcome_receipt(anchor["assistant_metadata_json"])
    except (ArchiveRecallOutcomeError, TypeError, ValueError) as exc:
        raise WorkItemAnchorError("archive assistant has no accepted outcome") from exc
    outcome = receipt.outcome
    if (
        outcome.plan_sha256 != plan_digest
        or receipt.outcome_sha256 != outcome_digest
        or not hmac.compare_digest(
            outcome.answer_sha256,
            _text_sha256(
                anchor["assistant_content"],
                label="archive answer",
                maximum_bytes=100_000,
            ),
        )
        or outcome.coverage_sha256 != evidence.coverage_sha256
        or outcome.coverage_grade.value != evidence.coverage_grade.value
        or (expected_lane is not None and outcome.lane is not expected_lane)
    ):
        raise WorkItemAnchorError("archive assistant outcome does not match its Work Item")

    selected = _archive_selection(evidence)
    source_bearing = outcome.status in _SOURCE_BEARING_STATUSES
    if expected_source_bearing is not None and source_bearing is not expected_source_bearing:
        raise WorkItemAnchorError("archive assistant outcome has the wrong replay result")
    if source_bearing:
        if outcome.selected_evidence != selected:
            raise WorkItemAnchorError("archive assistant selected evidence changed")
    elif outcome.status not in _SOURCE_FREE_STATUSES or outcome.selected_evidence is not None:
        raise WorkItemAnchorError("archive assistant outcome is not replayable or source-free")

    if outcome.lane is ArchiveRecallLane.FEDERATED_SEARCH:
        if not source_bearing:
            raise WorkItemAnchorError("archive search anchor has no selected evidence")
    elif outcome.lane is ArchiveRecallLane.SELECTED_EVIDENCE_REPLAY:
        if not hmac.compare_digest(
            outcome.plan_sha256,
            _replay_plan_sha256(anchor["boundary_content"], evidence),
        ):
            raise WorkItemAnchorError("archive replay plan does not match its selected evidence")
    else:  # pragma: no cover - the closed outcome parser rejects future lanes
        raise WorkItemAnchorError("archive assistant outcome lane is unsupported")


def _validate_immediate_followup(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    previous_assistant_message_id: str,
    boundary_user_message_id: str,
) -> None:
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    previous = _scope(
        previous_assistant_message_id,
        _MESSAGE_ID_RE,
        label="previous_assistant_message_id",
    )
    boundary = _scope(
        boundary_user_message_id,
        _MESSAGE_ID_RE,
        label="boundary_user_message_id",
    )
    row = conn.execute(
        """SELECT boundary.rowid
             FROM messages previous
             JOIN messages boundary
               ON boundary.user_id=previous.user_id
              AND boundary.conversation_id=previous.conversation_id
            WHERE previous.id=? AND previous.user_id=? AND previous.conversation_id=?
              AND previous.role='assistant' AND boundary.id=? AND boundary.role='user'
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
    ).fetchone()
    if row is None:
        raise WorkItemAnchorError("archive replay does not immediately follow its Work Item")


def _retire_active_conversation_work(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    now: str | None,
) -> str:
    timestamp = _now(now)
    cursor = conn.execute(
        """SELECT id,revision,updated_at,expires_at FROM work_items
            WHERE user_id=? AND conversation_id=? AND state='active' LIMIT 1""",
        (user_id, conversation_id),
    )
    row = cursor.fetchone()
    if row is None:
        return timestamp
    active = _row_mapping(cursor, row)
    revision = active["revision"]
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise WorkItemContractError("stored Work Item revision is invalid")
    timestamp = _logical_now(now, current_updated_at=str(active["updated_at"] or ""))
    expires = canonical_work_item_instant(active["expires_at"], label="expires_at")
    if expires != active["expires_at"]:
        raise WorkItemContractError("stored Work Item expiry is not canonical")
    if revision >= WORK_ITEM_MAX_REVISION:
        retired = conn.execute(
            """DELETE FROM work_items
                WHERE id=? AND user_id=? AND conversation_id=?
                  AND state='active' AND revision=?""",
            (active["id"], user_id, conversation_id, revision),
        )
    elif expires <= timestamp:
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
                user_id,
                conversation_id,
                revision,
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
                user_id,
                conversation_id,
                revision,
                timestamp,
            ),
        )
    if retired.rowcount != 1:
        raise WorkItemConflictError("active Work Item retirement lost its revision race")
    return timestamp


def create_recall_selected_archive_evidence_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    selected_evidence: SelectedArchiveEvidence,
    anchor_user_message_id: str,
    anchor_assistant_message_id: str,
    accepted_plan_sha256: str,
    accepted_outcome_sha256: str,
    now: str | None = None,
) -> RecallSelectedArchiveEvidenceWorkItem:
    """Atomically create the base row and its one immutable evidence sidecar."""

    _require_transaction(conn)
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    if type(selected_evidence) is not SelectedArchiveEvidence:
        raise WorkItemContractError("selected_evidence must use the exact typed contract")
    identifier = _scope(
        selected_evidence.work_item_id,
        _WORK_ITEM_ID_RE,
        label="work_item_id",
    )
    if selected_evidence.source_ref.principal_id != user:
        raise WorkItemAnchorError("selected archive evidence is not owned by the Work Item user")
    if selected_evidence.origin_boundary_user_message_id != anchor_user_message_id:
        raise WorkItemAnchorError("selected evidence origin does not match its publication")
    plan_digest = _digest(accepted_plan_sha256, label="accepted_plan_sha256")
    outcome_digest = _digest(accepted_outcome_sha256, label="accepted_outcome_sha256")
    _validate_archive_anchor(
        conn,
        user_id=user,
        conversation_id=conversation,
        boundary_user_message_id=anchor_user_message_id,
        assistant_message_id=anchor_assistant_message_id,
        accepted_plan_sha256=plan_digest,
        accepted_outcome_sha256=outcome_digest,
        evidence=selected_evidence,
        expected_lane=ArchiveRecallLane.FEDERATED_SEARCH,
        expected_source_bearing=True,
        require_latest_message=True,
    )
    timestamp = _retire_active_conversation_work(
        conn,
        user_id=user,
        conversation_id=conversation,
        now=now,
    )
    frame = RecallSelectedArchiveEvidenceActiveFrame()
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
                WorkKind.RECALL_SELECTED_ARCHIVE_EVIDENCE.value,
                WorkGoal.EXACT_SELECTED_ARCHIVE_EVIDENCE_RECALL.value,
                WorkState.ACTIVE.value,
                WorkPlaybook.RECALL_SELECTED_ARCHIVE_EVIDENCE.value,
                WorkCompletionContract.ACCEPTED_EXACT_SELECTED_ARCHIVE_EVIDENCE.value,
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
        conn.execute(
            """INSERT INTO work_item_selected_evidence(
                   work_item_id,corpus,source_ref_json,passage_refs_json,
                   source_snapshot_sha256,coverage_sha256,coverage_grade,
                   origin_boundary_user_message_id
               ) VALUES(:work_item_id,:corpus,:source_ref_json,:passage_refs_json,
                        :source_snapshot_sha256,:coverage_sha256,:coverage_grade,
                        :origin_boundary_user_message_id)""",
            selected_evidence.to_storage_payload(),
        )
    except sqlite3.IntegrityError as exc:
        raise WorkItemConflictError("archive Work Item creation lost its state race") from exc
    created = _fetch_archive_work_item(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if created is None:  # pragma: no cover - the transaction inserted both rows
        raise WorkItemConflictError("created archive Work Item is not durable")
    _validate_archive_anchor(
        conn,
        user_id=user,
        conversation_id=conversation,
        boundary_user_message_id=created.anchor_user_message_id,
        assistant_message_id=created.anchor_assistant_message_id,
        accepted_plan_sha256=created.accepted_plan_sha256,
        accepted_outcome_sha256=created.accepted_outcome_sha256,
        evidence=created.selected_evidence,
        expected_lane=ArchiveRecallLane.FEDERATED_SEARCH,
        expected_source_bearing=True,
        require_latest_message=True,
    )
    return created


def _anchor_expectations(
    item: RecallSelectedArchiveEvidenceWorkItem,
) -> tuple[ArchiveRecallLane | None, bool | None]:
    if item.transition is WorkTransition.CREATED:
        return ArchiveRecallLane.FEDERATED_SEARCH, True
    if item.transition is WorkTransition.EVIDENCE_REPLAYED:
        return ArchiveRecallLane.SELECTED_EVIDENCE_REPLAY, True
    return None, None


def _validate_stored_item_anchor(
    conn: sqlite3.Connection,
    item: RecallSelectedArchiveEvidenceWorkItem,
    *,
    require_latest_message: bool,
    allow_disabled_owner: bool = False,
) -> None:
    lane, source_bearing = _anchor_expectations(item)
    _validate_archive_anchor(
        conn,
        user_id=item.user_id,
        conversation_id=item.conversation_id,
        boundary_user_message_id=item.anchor_user_message_id,
        assistant_message_id=item.anchor_assistant_message_id,
        accepted_plan_sha256=item.accepted_plan_sha256,
        accepted_outcome_sha256=item.accepted_outcome_sha256,
        evidence=item.selected_evidence,
        expected_lane=lane,
        expected_source_bearing=source_bearing,
        require_latest_message=require_latest_message,
        allow_disabled_owner=allow_disabled_owner,
    )


def get_recall_selected_archive_evidence_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
) -> RecallSelectedArchiveEvidenceWorkItem | None:
    """Load one active-owner-scoped archive item and revalidate its anchor."""

    _require_transaction(conn)
    item = _fetch_archive_work_item(
        conn,
        work_item_id=_scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id"),
        user_id=_scope(user_id, _USER_ID_RE, label="user_id"),
        conversation_id=_scope(
            conversation_id,
            _CONVERSATION_ID_RE,
            label="conversation_id",
        ),
    )
    if item is not None:
        _validate_stored_item_anchor(conn, item, require_latest_message=False)
    return item


def get_recall_selected_archive_evidence_work_item_for_export_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
) -> RecallSelectedArchiveEvidenceWorkItem | None:
    """Integrity-check one owner export while admitting a disabled owner."""

    _require_transaction(conn)
    item = _fetch_archive_work_item(
        conn,
        work_item_id=_scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id"),
        user_id=_scope(user_id, _USER_ID_RE, label="user_id"),
        conversation_id=_scope(
            conversation_id,
            _CONVERSATION_ID_RE,
            label="conversation_id",
        ),
    )
    if item is not None:
        _validate_stored_item_anchor(
            conn,
            item,
            require_latest_message=False,
            allow_disabled_owner=True,
        )
    return item


def get_current_recall_selected_archive_evidence_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    boundary_user_message_id: str | None = None,
    now: str | None = None,
) -> RecallSelectedArchiveEvidenceWorkItem | None:
    """Load the one live archive item, optionally for its immediate follow-up."""

    _require_transaction(conn)
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    cursor = conn.execute(
        """SELECT id FROM work_items
            WHERE user_id=? AND conversation_id=?
              AND kind='recall_selected_archive_evidence'
              AND state='active' AND expires_at>? LIMIT 1""",
        (user, conversation, _now(now)),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    mapped = _row_mapping(cursor, row)
    identifier = _scope(mapped["id"], _WORK_ITEM_ID_RE, label="work_item_id")
    item = _fetch_archive_work_item(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if item is None:  # pragma: no cover - selected by the immediately prior query
        return None
    _validate_stored_item_anchor(
        conn,
        item,
        require_latest_message=boundary_user_message_id is None,
    )
    if boundary_user_message_id is not None:
        _validate_immediate_followup(
            conn,
            user_id=user,
            conversation_id=conversation,
            previous_assistant_message_id=item.anchor_assistant_message_id,
            boundary_user_message_id=boundary_user_message_id,
        )
        boundary = _scope(
            boundary_user_message_id,
            _MESSAGE_ID_RE,
            label="boundary_user_message_id",
        )
        boundary_row = conn.execute(
            "SELECT rowid FROM messages WHERE id=? AND user_id=? AND conversation_id=?",
            (boundary, user, conversation),
        ).fetchone()
        if (
            boundary_row is None
            or conn.execute(
                """SELECT 1 FROM messages
                WHERE user_id=? AND conversation_id=? AND rowid>? LIMIT 1""",
                (user, conversation, boundary_row[0]),
            ).fetchone()
            is not None
        ):
            raise WorkItemAnchorError("archive replay boundary is no longer latest")
    return item


def accept_recall_selected_archive_evidence_replay_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    new_boundary_user_message_id: str,
    new_assistant_message_id: str,
    new_accepted_plan_sha256: str,
    new_accepted_outcome_sha256: str,
    now: str | None = None,
) -> RecallSelectedArchiveEvidenceWorkItem:
    """CAS-accept exact replay, re-anchor it and refresh the bounded TTL."""

    return _accept_archive_replay_publication(
        conn,
        work_item_id=work_item_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
        new_boundary_user_message_id=new_boundary_user_message_id,
        new_assistant_message_id=new_assistant_message_id,
        new_accepted_plan_sha256=new_accepted_plan_sha256,
        new_accepted_outcome_sha256=new_accepted_outcome_sha256,
        source_bearing=True,
        now=now,
    )


def suspend_recall_selected_archive_evidence_replay_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    new_boundary_user_message_id: str,
    new_assistant_message_id: str,
    new_accepted_plan_sha256: str,
    new_accepted_outcome_sha256: str,
    now: str | None = None,
) -> RecallSelectedArchiveEvidenceWorkItem:
    """CAS-suspend after an accepted denied, drifted or unavailable replay."""

    return _accept_archive_replay_publication(
        conn,
        work_item_id=work_item_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
        new_boundary_user_message_id=new_boundary_user_message_id,
        new_assistant_message_id=new_assistant_message_id,
        new_accepted_plan_sha256=new_accepted_plan_sha256,
        new_accepted_outcome_sha256=new_accepted_outcome_sha256,
        source_bearing=False,
        now=now,
    )


def _accept_archive_replay_publication(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    new_boundary_user_message_id: str,
    new_assistant_message_id: str,
    new_accepted_plan_sha256: str,
    new_accepted_outcome_sha256: str,
    source_bearing: bool,
    now: str | None,
) -> RecallSelectedArchiveEvidenceWorkItem:
    _require_transaction(conn)
    revision = _revision(expected_revision)
    identifier = _scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    current = _fetch_archive_work_item(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if current is None or current.state is not WorkState.ACTIVE or current.revision != revision:
        raise WorkItemConflictError("archive Work Item revision/state is no longer current")
    timestamp = _logical_now(now, current_updated_at=current.updated_at)
    if current.expires_at <= timestamp:
        raise WorkItemConflictError("archive Work Item revision/state is no longer current")
    _validate_stored_item_anchor(conn, current, require_latest_message=False)
    _validate_immediate_followup(
        conn,
        user_id=user,
        conversation_id=conversation,
        previous_assistant_message_id=current.anchor_assistant_message_id,
        boundary_user_message_id=new_boundary_user_message_id,
    )
    plan_digest = _digest(new_accepted_plan_sha256, label="new_accepted_plan_sha256")
    outcome_digest = _digest(
        new_accepted_outcome_sha256,
        label="new_accepted_outcome_sha256",
    )
    _validate_archive_anchor(
        conn,
        user_id=user,
        conversation_id=conversation,
        boundary_user_message_id=new_boundary_user_message_id,
        assistant_message_id=new_assistant_message_id,
        accepted_plan_sha256=plan_digest,
        accepted_outcome_sha256=outcome_digest,
        evidence=current.selected_evidence,
        expected_lane=ArchiveRecallLane.SELECTED_EVIDENCE_REPLAY,
        expected_source_bearing=source_bearing,
        require_latest_message=True,
    )
    target_state = WorkState.ACTIVE if source_bearing else WorkState.SUSPENDED
    transition = WorkTransition.EVIDENCE_REPLAYED if source_bearing else WorkTransition.SUSPENDED
    expiry = _expiry(timestamp) if source_bearing else current.expires_at
    cursor = conn.execute(
        """UPDATE work_items
              SET state=?,transition=?,anchor_user_message_id=?,
                  anchor_assistant_message_id=?,accepted_plan_sha256=?,
                  accepted_outcome_sha256=?,revision=revision+1,
                  updated_at=?,expires_at=?,closed_at=NULL
            WHERE id=? AND user_id=? AND conversation_id=?
              AND kind='recall_selected_archive_evidence'
              AND state='active' AND revision=? AND expires_at>?""",
        (
            target_state.value,
            transition.value,
            new_boundary_user_message_id,
            new_assistant_message_id,
            plan_digest,
            outcome_digest,
            timestamp,
            expiry,
            identifier,
            user,
            conversation,
            revision,
            timestamp,
        ),
    )
    if cursor.rowcount != 1:
        raise WorkItemConflictError("archive replay CAS lost its revision race")
    updated = _fetch_archive_work_item(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if updated is None:  # pragma: no cover - same transaction updated the row
        raise WorkItemConflictError("replayed archive Work Item is not durable")
    _validate_archive_anchor(
        conn,
        user_id=user,
        conversation_id=conversation,
        boundary_user_message_id=updated.anchor_user_message_id,
        assistant_message_id=updated.anchor_assistant_message_id,
        accepted_plan_sha256=updated.accepted_plan_sha256,
        accepted_outcome_sha256=updated.accepted_outcome_sha256,
        evidence=updated.selected_evidence,
        expected_lane=ArchiveRecallLane.SELECTED_EVIDENCE_REPLAY,
        expected_source_bearing=source_bearing,
        require_latest_message=True,
    )
    return updated


def expire_recall_selected_archive_evidence_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    now: str | None = None,
) -> RecallSelectedArchiveEvidenceWorkItem:
    """CAS-expire one due active or suspended archive Work Item."""

    _require_transaction(conn)
    revision = _revision(expected_revision)
    identifier = _scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    current = _fetch_archive_work_item(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if (
        current is None
        or current.state not in {WorkState.ACTIVE, WorkState.SUSPENDED}
        or current.revision != revision
    ):
        raise WorkItemConflictError("archive Work Item revision/state is no longer current")
    timestamp = _logical_now(now, current_updated_at=current.updated_at)
    if current.expires_at > timestamp:
        raise WorkItemConflictError("archive Work Item revision/state is no longer current")
    _validate_stored_item_anchor(conn, current, require_latest_message=False)
    cursor = conn.execute(
        """UPDATE work_items
              SET state='expired',transition='expired',revision=revision+1,
                  updated_at=?,closed_at=?
            WHERE id=? AND user_id=? AND conversation_id=?
              AND kind='recall_selected_archive_evidence'
              AND state IN ('active','suspended') AND revision=? AND expires_at<=?""",
        (timestamp, timestamp, identifier, user, conversation, revision, timestamp),
    )
    if cursor.rowcount != 1:
        raise WorkItemConflictError("archive expiry CAS lost its revision race")
    updated = _fetch_archive_work_item(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if updated is None:  # pragma: no cover
        raise WorkItemConflictError("expired archive Work Item is not durable")
    return updated


def expire_due_recall_selected_archive_evidence_work_items_in_transaction(
    conn: sqlite3.Connection,
    *,
    now: str | None = None,
    user_id: str | None = None,
) -> int:
    """Expire all bounded due archive rows, optionally for one exact owner."""

    _require_transaction(conn)
    timestamp = _now(now)
    scope_sql = ""
    scope_parameters: tuple[object, ...] = ()
    if user_id is not None:
        scope_sql = "user_id=? AND "
        scope_parameters = (_scope(user_id, _USER_ID_RE, label="user_id"),)
    exhausted = conn.execute(
        f"""DELETE FROM work_items
              WHERE {scope_sql}kind='recall_selected_archive_evidence'
                AND state IN ('active','suspended') AND expires_at<=?
                AND revision>=2147483647""",  # nosec B608 - closed optional predicate
        (*scope_parameters, timestamp),
    )
    cursor = conn.execute(
        f"""UPDATE work_items
              SET state='expired',transition='expired',revision=revision+1,
                  updated_at=?,closed_at=?
            WHERE {scope_sql}kind='recall_selected_archive_evidence'
              AND state IN ('active','suspended') AND expires_at<=?
              AND revision<2147483647""",  # nosec B608 - closed optional predicate
        (timestamp, timestamp, *scope_parameters, timestamp),
    )
    return max(0, int(exhausted.rowcount or 0)) + max(0, int(cursor.rowcount or 0))


__all__ = [
    "accept_recall_selected_archive_evidence_replay_in_transaction",
    "create_recall_selected_archive_evidence_work_item_in_transaction",
    "expire_due_recall_selected_archive_evidence_work_items_in_transaction",
    "expire_recall_selected_archive_evidence_work_item_in_transaction",
    "get_current_recall_selected_archive_evidence_work_item_in_transaction",
    "get_recall_selected_archive_evidence_work_item_for_export_in_transaction",
    "get_recall_selected_archive_evidence_work_item_in_transaction",
    "new_recall_selected_archive_evidence_work_item_id",
    "suspend_recall_selected_archive_evidence_replay_in_transaction",
]
