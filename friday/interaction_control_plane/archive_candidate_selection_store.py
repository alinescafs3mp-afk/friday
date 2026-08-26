"""Transactional store for one durable archive candidate question.

Only stable source/passage identities and accepted-publication digests cross
this boundary.  Search text, display metadata, excerpts and model prose remain
in their authoritative stores and message ledger.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from friday.interaction_control_plane.archive_candidate_selection import (
    ARCHIVE_CANDIDATE_REASK_VERDICT_KIND,
    ArchiveCandidateOrdinalQuestion,
    ArchiveCandidateQuestionKind,
    ArchiveCandidateQuestionState,
    ArchiveCandidateSelectionActiveFrame,
    ArchiveCandidateSelectionError,
    ArchiveCandidateSelectionWorkItem,
    ArchiveCandidateSet,
    archive_candidate_reask_prompt,
    archive_candidate_selection_offer_suffix,
    parse_archive_candidate_ordinal,
)
from friday.interaction_control_plane.archive_evidence_work_item import (
    RecallSelectedArchiveEvidenceActiveFrame,
    RecallSelectedArchiveEvidenceWorkItem,
)
from friday.interaction_control_plane.archive_evidence_work_item_store import (
    _validate_archive_anchor,
    _validate_immediate_followup,
    get_recall_selected_archive_evidence_work_item_in_transaction,
)
from friday.interaction_control_plane.selected_archive_evidence import SelectedArchiveEvidence
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
    _begin_work_item_mutation_savepoint,
    _close_pending_compare_question_for_retirement,
    _release_work_item_mutation_savepoint,
    _rollback_work_item_mutation_savepoint,
)
from friday.orchestration.archive_recall_outcome import (
    ARCHIVE_EVIDENCE_REPLAY_UNAVAILABLE,
    ArchiveRecallLane,
    ArchiveRecallOutcomeError,
    ArchiveRecallStatus,
    load_accepted_archive_recall_outcome_receipt,
)
from friday.retrieval.archive_search_authority import (
    ArchiveSearchAcceptedCandidateProjection,
)

_WORK_ITEM_ID_RE = re.compile(r"work_[0-9a-f]{16}\Z")
_CANDIDATE_SET_ID_RE = re.compile(r"cset_[0-9a-f]{16}\Z")
_QUESTION_ID_RE = re.compile(r"question_[0-9a-f]{16}\Z")
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}\Z")
_MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{16}\Z")
_USER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")


def new_archive_candidate_selection_work_item_id() -> str:
    return f"work_{uuid.uuid4().hex[:16]}"


def new_archive_candidate_set_id() -> str:
    return f"cset_{uuid.uuid4().hex[:16]}"


def new_archive_candidate_question_id() -> str:
    return f"question_{uuid.uuid4().hex[:16]}"


def _require_transaction(conn: sqlite3.Connection) -> None:
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("candidate Work Item store requires a sqlite3 connection")
    if not conn.in_transaction:
        raise RuntimeError("candidate Work Item store requires an existing transaction")


def _scope(value: object, pattern: re.Pattern[str], *, label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise WorkItemContractError(f"{label} is not a canonical identifier")
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


def _logical_now(value: str | None, *, current_updated_at: str | None = None) -> str:
    timestamp = _now(value)
    if current_updated_at is None:
        return timestamp
    current = canonical_work_item_instant(current_updated_at, label="current_updated_at")
    if current != current_updated_at:
        raise WorkItemContractError("stored Work Item timestamp is not canonical")
    return max(timestamp, current)


def _expiry(now: str) -> str:
    return (datetime.fromisoformat(now) + timedelta(hours=WORK_ITEM_TTL_HOURS)).isoformat(timespec="seconds")


def _row_mapping(cursor: sqlite3.Cursor, row: object) -> dict[str, object]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if isinstance(row, Mapping):
        return {str(key): value for key, value in row.items()}
    if isinstance(row, tuple) and cursor.description is not None:
        return {str(column[0]): value for column, value in zip(cursor.description, row, strict=True)}
    raise WorkItemContractError("candidate Work Item query returned an invalid row")


def _text_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise WorkItemAnchorError(f"{label} is not text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise WorkItemAnchorError(f"{label} is not valid UTF-8") from exc
    if not encoded or len(encoded) > 100_000:
        raise WorkItemAnchorError(f"{label} exceeds the closed limit")
    return hashlib.sha256(encoded).hexdigest()


def _candidate_offer_is_exact(content: object, candidate_set: ArchiveCandidateSet) -> bool:
    if not isinstance(content, str):
        return False
    suffix = archive_candidate_selection_offer_suffix(
        tuple(item.public_citation_label for item in candidate_set.candidates)
    )
    delimiter = f"\n\n{suffix}"
    if not content.endswith(delimiter):
        return False
    model_answer = content[: -len(delimiter)]
    return bool(model_answer.strip() and not model_answer.endswith(("\r", "\n")))


def _validate_boundary_ordinal(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    boundary_user_message_id: str,
    expected_ordinal: int | None = None,
    reask_maximum_ordinal: int | None = None,
) -> None:
    """Bind one owned user boundary to either selection or the re-ask domain."""

    if (expected_ordinal is None) == (reask_maximum_ordinal is None):
        raise WorkItemAnchorError("candidate boundary ordinal validation mode is invalid")
    cursor = conn.execute(
        """SELECT content FROM messages
            WHERE id=? AND user_id=? AND conversation_id=? AND role='user'""",
        (boundary_user_message_id, user_id, conversation_id),
    )
    raw = cursor.fetchone()
    if raw is None:
        raise WorkItemAnchorError("candidate ordinal boundary is not owned")
    parsed = parse_archive_candidate_ordinal(_row_mapping(cursor, raw)["content"])
    if expected_ordinal is not None:
        if parsed != expected_ordinal:
            raise WorkItemAnchorError("candidate boundary ordinal does not match selection")
        return
    if reask_maximum_ordinal is None:  # pragma: no cover - mode check above proves this
        raise WorkItemAnchorError("candidate boundary ordinal validation mode is invalid")
    if parsed is not None and parsed <= reask_maximum_ordinal:
        raise WorkItemAnchorError("valid candidate ordinal cannot be re-asked")


def _fetch_candidate_work_item(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
) -> ArchiveCandidateSelectionWorkItem | None:
    work_cursor = conn.execute(
        """SELECT * FROM work_items
            WHERE id=? AND user_id=? AND conversation_id=?
              AND kind='select_archive_candidate_and_replay_evidence'""",
        (work_item_id, user_id, conversation_id),
    )
    raw_work = work_cursor.fetchone()
    if raw_work is None:
        return None
    set_cursor = conn.execute(
        "SELECT * FROM work_item_archive_candidate_sets WHERE work_item_id=?",
        (work_item_id,),
    )
    raw_set = set_cursor.fetchone()
    question_cursor = conn.execute(
        "SELECT * FROM work_item_archive_candidate_questions WHERE work_item_id=?",
        (work_item_id,),
    )
    raw_question = question_cursor.fetchone()
    if raw_set is None or raw_question is None:
        raise ArchiveCandidateSelectionError("candidate Work Item sidecars are missing")
    set_row = _row_mapping(set_cursor, raw_set)
    item_cursor = conn.execute(
        """SELECT * FROM work_item_archive_candidate_set_items
            WHERE candidate_set_id=? AND work_item_id=? ORDER BY ordinal""",
        (set_row["id"], work_item_id),
    )
    item_rows = [_row_mapping(item_cursor, row) for row in item_cursor.fetchall()]
    candidate_set = ArchiveCandidateSet.from_storage_rows(set_row, item_rows)
    question = ArchiveCandidateOrdinalQuestion.from_storage_row(_row_mapping(question_cursor, raw_question))
    return ArchiveCandidateSelectionWorkItem.from_storage_rows(
        _row_mapping(work_cursor, raw_work),
        candidate_set,
        question,
    )


def _validate_candidate_search_anchor(
    conn: sqlite3.Connection,
    item: ArchiveCandidateSelectionWorkItem,
    *,
    require_latest_message: bool,
    allow_disabled_owner: bool = False,
) -> None:
    cursor = conn.execute(
        """SELECT assistant.metadata_json AS assistant_metadata_json,
                  assistant.content AS assistant_content,
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
        (
            item.conversation_id,
            item.anchor_user_message_id,
            item.anchor_assistant_message_id,
            item.user_id,
            int(allow_disabled_owner),
        ),
    )
    raw = cursor.fetchone()
    if raw is None:
        raise WorkItemAnchorError("candidate publication anchor is not owned and exact")
    anchor = _row_mapping(cursor, raw)
    if require_latest_message:
        assistant_rowid = anchor["assistant_rowid"]
        if not isinstance(assistant_rowid, int) or isinstance(assistant_rowid, bool):
            raise WorkItemAnchorError("candidate assistant anchor row is invalid")
        later = conn.execute(
            """SELECT 1 FROM messages
                WHERE user_id=? AND conversation_id=? AND rowid>? LIMIT 1""",
            (item.user_id, item.conversation_id, assistant_rowid),
        ).fetchone()
        if later is not None:
            raise WorkItemAnchorError("candidate assistant anchor is not the latest publication")
    try:
        receipt = load_accepted_archive_recall_outcome_receipt(anchor["assistant_metadata_json"])
    except (ArchiveRecallOutcomeError, TypeError, ValueError) as exc:
        raise WorkItemAnchorError("candidate assistant has no accepted archive outcome") from exc
    outcome = receipt.outcome
    candidate_set = item.candidate_set
    if (
        outcome.lane is not ArchiveRecallLane.FEDERATED_SEARCH
        or outcome.status not in {ArchiveRecallStatus.COMPLETE, ArchiveRecallStatus.PARTIAL}
        or outcome.plan_sha256 != item.accepted_plan_sha256
        or receipt.outcome_sha256 != item.accepted_outcome_sha256
        or not hmac.compare_digest(
            outcome.answer_sha256,
            _text_sha256(anchor["assistant_content"], label="candidate answer"),
        )
        or outcome.evidence_sha256 != candidate_set.evidence_sha256
        or outcome.coverage_sha256 != candidate_set.coverage_sha256
        or outcome.coverage_grade.value != candidate_set.coverage_grade.value
        or outcome.candidate_projection_sha256 != candidate_set.authority_projection_sha256
        or outcome.candidate_count < len(candidate_set.candidates)
        or outcome.selected_evidence is not None
        or not _candidate_offer_is_exact(anchor["assistant_content"], candidate_set)
    ):
        raise WorkItemAnchorError("candidate assistant outcome does not match its closed set")


def _validate_source_free_question_publication(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    previous_assistant_message_id: str | None,
    origin_assistant_message_id: str,
    boundary_user_message_id: str,
    assistant_message_id: str,
    maximum_ordinal: int,
    require_latest_message: bool,
    allow_disabled_owner: bool = False,
) -> None:
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    origin = _scope(
        origin_assistant_message_id,
        _MESSAGE_ID_RE,
        label="origin_assistant_message_id",
    )
    boundary = _scope(
        boundary_user_message_id,
        _MESSAGE_ID_RE,
        label="question_boundary_user_message_id",
    )
    assistant = _scope(
        assistant_message_id,
        _MESSAGE_ID_RE,
        label="question_assistant_message_id",
    )
    expected_content = archive_candidate_reask_prompt(maximum_ordinal)
    _validate_boundary_ordinal(
        conn,
        user_id=user,
        conversation_id=conversation,
        boundary_user_message_id=boundary,
        reask_maximum_ordinal=maximum_ordinal,
    )
    if previous_assistant_message_id is not None:
        _validate_immediate_followup(
            conn,
            user_id=user,
            conversation_id=conversation,
            previous_assistant_message_id=previous_assistant_message_id,
            boundary_user_message_id=boundary,
        )
    cursor = conn.execute(
        """SELECT assistant.rowid AS assistant_rowid
             FROM users owner
             JOIN conversations conversation
               ON conversation.id=? AND conversation.user_id=owner.id
             JOIN messages origin
               ON origin.id=? AND origin.user_id=owner.id
              AND origin.conversation_id=conversation.id AND origin.role='assistant'
             JOIN messages boundary
               ON boundary.id=? AND boundary.user_id=owner.id
              AND boundary.conversation_id=conversation.id AND boundary.role='user'
              AND origin.rowid<boundary.rowid
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
              AND assistant.content=?
              AND json_extract(assistant.metadata_json,
                               '$.structural.verdict_kind')=?
              AND json_extract(assistant.metadata_json,
                               '$.structural.answer_present')=1
              AND json_extract(assistant.metadata_json,
                               '$.structural.model_spoke')=0
              AND json_extract(assistant.metadata_json,
                               '$.accepted_archive_recall_outcome') IS NULL
              AND json_extract(assistant.metadata_json,
                               '$.accepted_capability_outcome') IS NULL
              AND json_extract(assistant.metadata_json,
                               '$.accepted_simple_public_news_outcome') IS NULL
              AND COALESCE(json_extract(assistant.metadata_json,
                                        '$.structural.model_spoke'),0)=0
            LIMIT 1""",
        (
            conversation,
            origin,
            boundary,
            assistant,
            user,
            int(allow_disabled_owner),
            expected_content,
            ARCHIVE_CANDIDATE_REASK_VERDICT_KIND,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise WorkItemAnchorError("candidate re-ask publication is not source-free and exact")
    if require_latest_message:
        mapped = _row_mapping(cursor, row)
        assistant_rowid = mapped["assistant_rowid"]
        if not isinstance(assistant_rowid, int) or isinstance(assistant_rowid, bool):
            raise WorkItemAnchorError("candidate re-ask assistant row is invalid")
        if (
            conn.execute(
                """SELECT 1 FROM messages
                WHERE user_id=? AND conversation_id=? AND rowid>? LIMIT 1""",
                (user, conversation, assistant_rowid),
            ).fetchone()
            is not None
        ):
            raise WorkItemAnchorError("candidate re-ask is not the latest publication")


def _validate_source_free_replay_failure_publication(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    boundary_user_message_id: str,
    assistant_message_id: str,
    accepted_plan_sha256: str,
    accepted_outcome_sha256: str,
    evidence: SelectedArchiveEvidence,
    require_latest_message: bool,
    allow_disabled_owner: bool = False,
) -> None:
    """Validate one exact accepted replay failure and its code-owned source-free body."""

    _validate_archive_anchor(
        conn,
        user_id=user_id,
        conversation_id=conversation_id,
        boundary_user_message_id=boundary_user_message_id,
        assistant_message_id=assistant_message_id,
        accepted_plan_sha256=accepted_plan_sha256,
        accepted_outcome_sha256=accepted_outcome_sha256,
        evidence=evidence,
        expected_lane=ArchiveRecallLane.SELECTED_EVIDENCE_REPLAY,
        expected_source_bearing=False,
        require_latest_message=require_latest_message,
        allow_disabled_owner=allow_disabled_owner,
    )
    if (
        conn.execute(
            """SELECT 1 FROM messages
            WHERE id=? AND user_id=? AND conversation_id=?
              AND role='assistant' AND content=?""",
            (
                assistant_message_id,
                user_id,
                conversation_id,
                ARCHIVE_EVIDENCE_REPLAY_UNAVAILABLE,
            ),
        ).fetchone()
        is None
    ):
        raise WorkItemAnchorError("candidate replay failure body is not source-free and exact")


def _validate_stored_anchor(
    conn: sqlite3.Connection,
    item: ArchiveCandidateSelectionWorkItem,
    *,
    require_latest_message: bool,
    allow_disabled_owner: bool = False,
) -> None:
    _validate_candidate_search_anchor(
        conn,
        item,
        require_latest_message=(
            require_latest_message
            and item.state is WorkState.WAITING_FOR_INPUT
            and item.question.prompt_assistant_message_id == item.anchor_assistant_message_id
        ),
        allow_disabled_owner=allow_disabled_owner,
    )
    selected = item.selected_evidence
    question = item.question
    if question.prompt_assistant_message_id != item.anchor_assistant_message_id:
        _validate_source_free_question_publication(
            conn,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            previous_assistant_message_id=None,
            origin_assistant_message_id=item.anchor_assistant_message_id,
            boundary_user_message_id=question.prompt_boundary_user_message_id,
            assistant_message_id=question.prompt_assistant_message_id,
            maximum_ordinal=question.maximum_ordinal,
            require_latest_message=(require_latest_message and item.state is WorkState.WAITING_FOR_INPUT),
            allow_disabled_owner=allow_disabled_owner,
        )
    if item.transition is WorkTransition.CANDIDATE_REPLAYED and selected is not None:
        if (
            question.replay_boundary_user_message_id is None
            or question.replay_assistant_message_id is None
            or question.accepted_replay_plan_sha256 is None
            or question.accepted_replay_outcome_sha256 is None
        ):  # pragma: no cover - the joined typed contract already rejects this
            raise WorkItemAnchorError("candidate replay receipt is missing")
        _validate_immediate_followup(
            conn,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            previous_assistant_message_id=question.prompt_assistant_message_id,
            boundary_user_message_id=question.replay_boundary_user_message_id,
        )
        _validate_boundary_ordinal(
            conn,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            boundary_user_message_id=question.replay_boundary_user_message_id,
            expected_ordinal=question.selected_ordinal,
        )
        _validate_archive_anchor(
            conn,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            boundary_user_message_id=question.replay_boundary_user_message_id,
            assistant_message_id=question.replay_assistant_message_id,
            accepted_plan_sha256=question.accepted_replay_plan_sha256,
            accepted_outcome_sha256=question.accepted_replay_outcome_sha256,
            evidence=selected,
            expected_lane=ArchiveRecallLane.SELECTED_EVIDENCE_REPLAY,
            expected_source_bearing=True,
            require_latest_message=require_latest_message,
            allow_disabled_owner=allow_disabled_owner,
        )
    failed = item.failed_evidence
    if question.has_replay_failure_receipt and failed is not None:
        if (
            question.failure_boundary_user_message_id is None
            or question.failure_assistant_message_id is None
            or question.accepted_failure_plan_sha256 is None
            or question.accepted_failure_outcome_sha256 is None
        ):  # pragma: no cover - the joined typed contract already rejects this
            raise WorkItemAnchorError("candidate replay failure receipt is missing")
        _validate_immediate_followup(
            conn,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            previous_assistant_message_id=question.prompt_assistant_message_id,
            boundary_user_message_id=question.failure_boundary_user_message_id,
        )
        _validate_boundary_ordinal(
            conn,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            boundary_user_message_id=question.failure_boundary_user_message_id,
            expected_ordinal=question.failed_ordinal,
        )
        _validate_source_free_replay_failure_publication(
            conn,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            boundary_user_message_id=question.failure_boundary_user_message_id,
            assistant_message_id=question.failure_assistant_message_id,
            accepted_plan_sha256=question.accepted_failure_plan_sha256,
            accepted_outcome_sha256=question.accepted_failure_outcome_sha256,
            evidence=failed,
            require_latest_message=require_latest_message,
            allow_disabled_owner=allow_disabled_owner,
        )


def _retire_open_conversation_work(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    now: str | None,
) -> str:
    timestamp = _now(now)
    cursor = conn.execute(
        """SELECT id,state,revision,updated_at,expires_at FROM work_items
            WHERE user_id=? AND conversation_id=?
              AND state IN ('active','waiting_for_input') LIMIT 1""",
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
                  AND state=? AND revision=?""",
            (active["id"], user_id, conversation_id, active["state"], revision),
        )
    elif expires <= timestamp:
        _close_pending_compare_question_for_retirement(
            conn,
            work_item_id=active["id"],
            state=active["state"],
            revision=revision,
            closed_at=timestamp,
            close_reason=WorkState.EXPIRED.value,
        )
        retired = conn.execute(
            """UPDATE work_items
                  SET state='expired',transition='expired',revision=revision+1,
                      updated_at=?,closed_at=?
                WHERE id=? AND user_id=? AND conversation_id=?
                  AND state=? AND revision=? AND expires_at<=?""",
            (
                timestamp,
                timestamp,
                active["id"],
                user_id,
                conversation_id,
                active["state"],
                revision,
                timestamp,
            ),
        )
    else:
        _close_pending_compare_question_for_retirement(
            conn,
            work_item_id=active["id"],
            state=active["state"],
            revision=revision,
            closed_at=timestamp,
            close_reason=WorkState.SUSPENDED.value,
        )
        retired = conn.execute(
            """UPDATE work_items
                  SET state='suspended',transition='suspended',revision=revision+1,
                      updated_at=?,closed_at=NULL
                WHERE id=? AND user_id=? AND conversation_id=?
                  AND state=? AND revision=? AND expires_at>?""",
            (
                timestamp,
                active["id"],
                user_id,
                conversation_id,
                active["state"],
                revision,
                timestamp,
            ),
        )
    if retired.rowcount != 1:
        raise WorkItemConflictError("open Work Item retirement lost its revision race")
    return timestamp


def create_archive_candidate_selection_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    accepted_candidate_projection: ArchiveSearchAcceptedCandidateProjection,
    work_item_id: str | None = None,
    candidate_set_id: str | None = None,
    question_id: str | None = None,
    anchor_user_message_id: str,
    anchor_assistant_message_id: str,
    accepted_plan_sha256: str,
    accepted_outcome_sha256: str,
    now: str | None = None,
) -> ArchiveCandidateSelectionWorkItem:
    """Create one closed set and its typed unanswered question atomically."""

    _require_transaction(conn)
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    if type(accepted_candidate_projection) is not ArchiveSearchAcceptedCandidateProjection:
        raise WorkItemContractError("candidate selection requires the exact sealed authority projection")
    identifier = _scope(
        work_item_id or new_archive_candidate_selection_work_item_id(),
        _WORK_ITEM_ID_RE,
        label="work_item_id",
    )
    set_identifier = _scope(
        candidate_set_id or new_archive_candidate_set_id(),
        _CANDIDATE_SET_ID_RE,
        label="candidate_set_id",
    )
    question_identifier = _scope(
        question_id or new_archive_candidate_question_id(),
        _QUESTION_ID_RE,
        label="question_id",
    )
    boundary = _scope(anchor_user_message_id, _MESSAGE_ID_RE, label="anchor_user_message_id")
    assistant = _scope(
        anchor_assistant_message_id,
        _MESSAGE_ID_RE,
        label="anchor_assistant_message_id",
    )
    candidate_set = ArchiveCandidateSet.from_accepted_projection(
        id=set_identifier,
        work_item_id=identifier,
        origin_boundary_user_message_id=boundary,
        projection=accepted_candidate_projection,
    )
    if any(candidate.source_ref.principal_id != user for candidate in candidate_set.candidates):
        raise WorkItemAnchorError("candidate set contains a foreign source")
    plan_digest = _digest(accepted_plan_sha256, label="accepted_plan_sha256")
    outcome_digest = _digest(accepted_outcome_sha256, label="accepted_outcome_sha256")
    preliminary_time = _now(now)
    preliminary_question = ArchiveCandidateOrdinalQuestion(
        id=question_identifier,
        work_item_id=identifier,
        candidate_set_id=set_identifier,
        kind=ArchiveCandidateQuestionKind.SELECT_ORDINAL,
        minimum_ordinal=1,
        maximum_ordinal=len(candidate_set.candidates),
        state=ArchiveCandidateQuestionState.WAITING,
        selected_ordinal=None,
        created_at=preliminary_time,
        prompt_boundary_user_message_id=boundary,
        prompt_assistant_message_id=assistant,
        prompt_updated_at=preliminary_time,
        prompt_revision=1,
        answered_at=None,
        replay_boundary_user_message_id=None,
        replay_assistant_message_id=None,
        accepted_replay_plan_sha256=None,
        accepted_replay_outcome_sha256=None,
        failed_ordinal=None,
        failure_boundary_user_message_id=None,
        failure_assistant_message_id=None,
        failure_recorded_at=None,
        accepted_failure_plan_sha256=None,
        accepted_failure_outcome_sha256=None,
    )
    preliminary = ArchiveCandidateSelectionWorkItem(
        id=identifier,
        user_id=user,
        conversation_id=conversation,
        state=WorkState.WAITING_FOR_INPUT,
        active_frame=ArchiveCandidateSelectionActiveFrame(),
        anchor_user_message_id=boundary,
        anchor_assistant_message_id=assistant,
        accepted_plan_sha256=plan_digest,
        accepted_outcome_sha256=outcome_digest,
        revision=1,
        transition=WorkTransition.QUESTION_ASKED,
        created_at=preliminary_time,
        updated_at=preliminary_time,
        expires_at=_expiry(preliminary_time),
        closed_at=None,
        candidate_set=candidate_set,
        question=preliminary_question,
    )
    _validate_candidate_search_anchor(
        conn,
        preliminary,
        require_latest_message=True,
    )
    savepoint = _begin_work_item_mutation_savepoint(conn)
    try:
        timestamp = _retire_open_conversation_work(
            conn,
            user_id=user,
            conversation_id=conversation,
            now=now,
        )
        question = ArchiveCandidateOrdinalQuestion(
            id=question_identifier,
            work_item_id=identifier,
            candidate_set_id=set_identifier,
            kind=ArchiveCandidateQuestionKind.SELECT_ORDINAL,
            minimum_ordinal=1,
            maximum_ordinal=len(candidate_set.candidates),
            state=ArchiveCandidateQuestionState.WAITING,
            selected_ordinal=None,
            created_at=timestamp,
            prompt_boundary_user_message_id=boundary,
            prompt_assistant_message_id=assistant,
            prompt_updated_at=timestamp,
            prompt_revision=1,
            answered_at=None,
            replay_boundary_user_message_id=None,
            replay_assistant_message_id=None,
            accepted_replay_plan_sha256=None,
            accepted_replay_outcome_sha256=None,
            failed_ordinal=None,
            failure_boundary_user_message_id=None,
            failure_assistant_message_id=None,
            failure_recorded_at=None,
            accepted_failure_plan_sha256=None,
            accepted_failure_outcome_sha256=None,
        )
        frame = ArchiveCandidateSelectionActiveFrame()
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
                WorkKind.SELECT_ARCHIVE_CANDIDATE_AND_REPLAY_EVIDENCE.value,
                WorkGoal.EXACT_ARCHIVE_CANDIDATE_SELECTION_AND_EVIDENCE_REPLAY.value,
                WorkState.WAITING_FOR_INPUT.value,
                WorkPlaybook.SELECT_ARCHIVE_CANDIDATE_AND_REPLAY_EVIDENCE.value,
                WorkCompletionContract.ACCEPTED_EXACT_ARCHIVE_CANDIDATE_AND_EVIDENCE_REPLAY.value,
                frame.to_json(),
                boundary,
                assistant,
                plan_digest,
                outcome_digest,
                1,
                WorkTransition.QUESTION_ASKED.value,
                timestamp,
                timestamp,
                _expiry(timestamp),
            ),
        )
        conn.execute(
            """INSERT INTO work_item_archive_candidate_sets(
                   id,work_item_id,evidence_sha256,coverage_sha256,coverage_grade,
                   authority_projection_sha256,origin_boundary_user_message_id,
                   candidate_set_sha256
               ) VALUES(:id,:work_item_id,:evidence_sha256,:coverage_sha256,:coverage_grade,
                        :authority_projection_sha256,:origin_boundary_user_message_id,
                        :candidate_set_sha256)""",
            candidate_set.set_storage_payload(),
        )
        for payload in candidate_set.item_storage_payloads():
            conn.execute(
                """INSERT INTO work_item_archive_candidate_set_items(
                       candidate_set_id,work_item_id,ordinal,public_citation_label,
                       corpus,source_ref_json,passage_refs_json,source_snapshot_sha256
                   ) VALUES(:candidate_set_id,:work_item_id,:ordinal,:public_citation_label,
                            :corpus,:source_ref_json,:passage_refs_json,
                            :source_snapshot_sha256)""",
                payload,
            )
        conn.execute(
            """INSERT INTO work_item_archive_candidate_questions(
                   id,work_item_id,candidate_set_id,kind,minimum_ordinal,
                   maximum_ordinal,state,selected_ordinal,created_at,
                   prompt_boundary_user_message_id,prompt_assistant_message_id,
                   prompt_updated_at,prompt_revision,answered_at,
                   replay_boundary_user_message_id,replay_assistant_message_id,
                   accepted_replay_plan_sha256,accepted_replay_outcome_sha256,
                   failed_ordinal,failure_boundary_user_message_id,
                   failure_assistant_message_id,failure_recorded_at,
                   accepted_failure_plan_sha256,accepted_failure_outcome_sha256
               ) VALUES(:id,:work_item_id,:candidate_set_id,:kind,:minimum_ordinal,
                        :maximum_ordinal,:state,:selected_ordinal,:created_at,
                        :prompt_boundary_user_message_id,:prompt_assistant_message_id,
                        :prompt_updated_at,:prompt_revision,:answered_at,
                        :replay_boundary_user_message_id,:replay_assistant_message_id,
                        :accepted_replay_plan_sha256,:accepted_replay_outcome_sha256,
                        :failed_ordinal,:failure_boundary_user_message_id,
                        :failure_assistant_message_id,:failure_recorded_at,
                        :accepted_failure_plan_sha256,:accepted_failure_outcome_sha256)""",
            question.storage_payload(),
        )
        created = _fetch_candidate_work_item(
            conn,
            work_item_id=identifier,
            user_id=user,
            conversation_id=conversation,
        )
        if created is None:  # pragma: no cover - this transaction inserted every row
            raise WorkItemConflictError("created candidate Work Item is not durable")
        _validate_candidate_search_anchor(conn, created, require_latest_message=True)
    except BaseException as exc:
        _rollback_work_item_mutation_savepoint(conn, savepoint)
        if isinstance(exc, sqlite3.IntegrityError):
            raise WorkItemConflictError("candidate Work Item creation lost its state race") from exc
        raise
    _release_work_item_mutation_savepoint(conn, savepoint)
    return created


def get_archive_candidate_selection_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
) -> ArchiveCandidateSelectionWorkItem | None:
    _require_transaction(conn)
    item = _fetch_candidate_work_item(
        conn,
        work_item_id=_scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id"),
        user_id=_scope(user_id, _USER_ID_RE, label="user_id"),
        conversation_id=_scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id"),
    )
    if item is not None:
        _validate_stored_anchor(conn, item, require_latest_message=False)
    return item


def get_archive_candidate_selection_work_item_for_export_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
) -> ArchiveCandidateSelectionWorkItem | None:
    _require_transaction(conn)
    item = _fetch_candidate_work_item(
        conn,
        work_item_id=_scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id"),
        user_id=_scope(user_id, _USER_ID_RE, label="user_id"),
        conversation_id=_scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id"),
    )
    if item is not None:
        _validate_stored_anchor(
            conn,
            item,
            require_latest_message=False,
            allow_disabled_owner=True,
        )
    return item


def get_current_archive_candidate_selection_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    boundary_user_message_id: str | None = None,
    now: str | None = None,
) -> ArchiveCandidateSelectionWorkItem | None:
    item = get_waiting_archive_candidate_selection_work_item_in_transaction(
        conn,
        user_id=user_id,
        conversation_id=conversation_id,
        boundary_user_message_id=boundary_user_message_id,
    )
    if item is None or item.expires_at <= _now(now):
        return None
    return item


def get_waiting_archive_candidate_selection_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    boundary_user_message_id: str | None = None,
    require_latest_message: bool = True,
) -> ArchiveCandidateSelectionWorkItem | None:
    """Return the exact open question, including one whose bounded TTL elapsed."""

    _require_transaction(conn)
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    cursor = conn.execute(
        """SELECT id FROM work_items
            WHERE user_id=? AND conversation_id=?
              AND kind='select_archive_candidate_and_replay_evidence'
              AND state='waiting_for_input' LIMIT 1""",
        (user, conversation),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    identifier = _scope(_row_mapping(cursor, row)["id"], _WORK_ITEM_ID_RE, label="work_item_id")
    item = _fetch_candidate_work_item(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if item is None:  # pragma: no cover - selected immediately above
        return None
    _validate_stored_anchor(
        conn,
        item,
        require_latest_message=(boundary_user_message_id is None and require_latest_message),
    )
    if boundary_user_message_id is not None:
        _validate_immediate_followup(
            conn,
            user_id=user,
            conversation_id=conversation,
            previous_assistant_message_id=item.question.prompt_assistant_message_id,
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
            raise WorkItemAnchorError("candidate selection boundary is no longer latest")
    return item


def archive_candidate_selection_is_displaced_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
) -> bool:
    """Read whether another durable message already superseded the prompt."""

    _require_transaction(conn)
    identifier = _scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    current = _fetch_candidate_work_item(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if (
        current is None
        or current.state is not WorkState.WAITING_FOR_INPUT
        or current.revision != _revision(expected_revision)
    ):
        raise WorkItemConflictError("candidate Work Item revision/state is no longer current")
    _validate_stored_anchor(conn, current, require_latest_message=False)
    prompt_row = conn.execute(
        """SELECT rowid FROM messages
             WHERE id=? AND user_id=? AND conversation_id=? AND role='assistant'""",
        (current.question.prompt_assistant_message_id, user, conversation),
    ).fetchone()
    if prompt_row is None:  # pragma: no cover - validated directly above
        raise WorkItemAnchorError("candidate prompt assistant is unavailable")
    displaced = conn.execute(
        """SELECT 1 FROM messages
             WHERE user_id=? AND conversation_id=? AND rowid>? LIMIT 1""",
        (user, conversation, int(prompt_row[0])),
    ).fetchone()
    return displaced is not None


def retire_displaced_archive_candidate_selection_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    now: str | None = None,
) -> ArchiveCandidateSelectionWorkItem | None:
    """Lazily close a question only after another durable turn displaced it."""

    displaced = archive_candidate_selection_is_displaced_in_transaction(
        conn,
        work_item_id=work_item_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
    )
    if not displaced:
        return None
    current = _fetch_candidate_work_item(
        conn,
        work_item_id=_scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id"),
        user_id=_scope(user_id, _USER_ID_RE, label="user_id"),
        conversation_id=_scope(
            conversation_id,
            _CONVERSATION_ID_RE,
            label="conversation_id",
        ),
    )
    if current is None:  # pragma: no cover - validated by the read above
        raise WorkItemConflictError("candidate Work Item revision/state is no longer current")
    timestamp = _now(now)
    mutation = (
        expire_archive_candidate_selection_in_transaction
        if current.expires_at <= timestamp
        else cancel_archive_candidate_selection_in_transaction
    )
    return mutation(
        conn,
        work_item_id=current.id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=current.revision,
        now=timestamp,
    )


def reask_archive_candidate_selection_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    invalid_boundary_user_message_id: str,
    new_question_assistant_message_id: str,
    now: str | None = None,
) -> ArchiveCandidateSelectionWorkItem:
    """CAS-publish a source-free deterministic re-ask without changing candidate proof."""

    _require_transaction(conn)
    revision = _revision(expected_revision)
    identifier = _scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    boundary = _scope(
        invalid_boundary_user_message_id,
        _MESSAGE_ID_RE,
        label="invalid_boundary_user_message_id",
    )
    assistant = _scope(
        new_question_assistant_message_id,
        _MESSAGE_ID_RE,
        label="new_question_assistant_message_id",
    )
    current = _fetch_candidate_work_item(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if (
        current is None
        or current.state is not WorkState.WAITING_FOR_INPUT
        or current.revision != revision
        or current.question.prompt_revision != revision
    ):
        raise WorkItemConflictError("candidate Work Item revision/state is no longer current")
    timestamp = _logical_now(now, current_updated_at=current.updated_at)
    if current.expires_at <= timestamp:
        raise WorkItemConflictError("candidate Work Item revision/state is no longer current")
    _validate_stored_anchor(conn, current, require_latest_message=False)
    _validate_source_free_question_publication(
        conn,
        user_id=user,
        conversation_id=conversation,
        previous_assistant_message_id=current.question.prompt_assistant_message_id,
        origin_assistant_message_id=current.anchor_assistant_message_id,
        boundary_user_message_id=boundary,
        assistant_message_id=assistant,
        maximum_ordinal=current.question.maximum_ordinal,
        require_latest_message=True,
    )
    expiry = _expiry(timestamp)
    savepoint = _begin_work_item_mutation_savepoint(conn)
    try:
        question_cursor = conn.execute(
            """UPDATE work_item_archive_candidate_questions
                  SET prompt_boundary_user_message_id=?,prompt_assistant_message_id=?,
                      prompt_updated_at=?,prompt_revision=prompt_revision+1
                WHERE id=? AND work_item_id=? AND candidate_set_id=?
                  AND state='waiting' AND selected_ordinal IS NULL AND answered_at IS NULL
                  AND prompt_revision=? AND prompt_assistant_message_id=?""",
            (
                boundary,
                assistant,
                timestamp,
                current.question.id,
                identifier,
                current.candidate_set.id,
                revision,
                current.question.prompt_assistant_message_id,
            ),
        )
        if question_cursor.rowcount != 1:
            raise WorkItemConflictError("candidate re-ask question CAS lost its state race")
        work_cursor = conn.execute(
            """UPDATE work_items
                  SET transition='question_reasked',revision=revision+1,
                      updated_at=?,expires_at=?
                WHERE id=? AND user_id=? AND conversation_id=?
                  AND kind='select_archive_candidate_and_replay_evidence'
                  AND state='waiting_for_input' AND revision=? AND expires_at>?""",
            (
                timestamp,
                expiry,
                identifier,
                user,
                conversation,
                revision,
                timestamp,
            ),
        )
        if work_cursor.rowcount != 1:
            raise WorkItemConflictError("candidate re-ask CAS lost its revision race")
        updated = _fetch_candidate_work_item(
            conn,
            work_item_id=identifier,
            user_id=user,
            conversation_id=conversation,
        )
        if updated is None:  # pragma: no cover - updated in this transaction
            raise WorkItemConflictError("re-asked candidate Work Item is not durable")
        _validate_stored_anchor(conn, updated, require_latest_message=True)
    except BaseException:
        _rollback_work_item_mutation_savepoint(conn, savepoint)
        raise
    _release_work_item_mutation_savepoint(conn, savepoint)
    return updated


def accept_archive_candidate_selection_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    selected_ordinal: int,
    new_boundary_user_message_id: str,
    new_assistant_message_id: str,
    new_accepted_plan_sha256: str,
    new_accepted_outcome_sha256: str,
    now: str | None = None,
) -> ArchiveCandidateSelectionWorkItem:
    """CAS-select one exact ordinal only after its replay publication is accepted."""

    return _accept_archive_candidate_selection(
        conn,
        work_item_id=work_item_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
        selected_ordinal=selected_ordinal,
        new_boundary_user_message_id=new_boundary_user_message_id,
        new_assistant_message_id=new_assistant_message_id,
        new_accepted_plan_sha256=new_accepted_plan_sha256,
        new_accepted_outcome_sha256=new_accepted_outcome_sha256,
        now=now,
        manage_savepoint=True,
    )


def _accept_archive_candidate_selection(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    selected_ordinal: int,
    new_boundary_user_message_id: str,
    new_assistant_message_id: str,
    new_accepted_plan_sha256: str,
    new_accepted_outcome_sha256: str,
    now: str | None,
    manage_savepoint: bool,
) -> ArchiveCandidateSelectionWorkItem:
    """Validate and mutate one selection, optionally owning its savepoint."""

    _require_transaction(conn)
    revision = _revision(expected_revision)
    identifier = _scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    current = _fetch_candidate_work_item(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if current is None or current.state is not WorkState.WAITING_FOR_INPUT or current.revision != revision:
        raise WorkItemConflictError("candidate Work Item revision/state is no longer current")
    timestamp = _logical_now(now, current_updated_at=current.updated_at)
    if current.expires_at <= timestamp:
        raise WorkItemConflictError("candidate Work Item revision/state is no longer current")
    try:
        selected = current.candidate_set.selected_evidence(selected_ordinal)
    except ArchiveCandidateSelectionError as exc:
        raise WorkItemConflictError("candidate ordinal is not in the closed set") from exc
    _validate_stored_anchor(conn, current, require_latest_message=False)
    _validate_immediate_followup(
        conn,
        user_id=user,
        conversation_id=conversation,
        previous_assistant_message_id=current.question.prompt_assistant_message_id,
        boundary_user_message_id=new_boundary_user_message_id,
    )
    _validate_boundary_ordinal(
        conn,
        user_id=user,
        conversation_id=conversation,
        boundary_user_message_id=new_boundary_user_message_id,
        expected_ordinal=selected_ordinal,
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
        evidence=selected,
        expected_lane=ArchiveRecallLane.SELECTED_EVIDENCE_REPLAY,
        expected_source_bearing=True,
        require_latest_message=True,
    )
    savepoint = _begin_work_item_mutation_savepoint(conn) if manage_savepoint else None
    try:
        question_cursor = conn.execute(
            """UPDATE work_item_archive_candidate_questions
                  SET state='answered',selected_ordinal=?,answered_at=?,
                      replay_boundary_user_message_id=?,replay_assistant_message_id=?,
                      accepted_replay_plan_sha256=?,accepted_replay_outcome_sha256=?
                WHERE id=? AND work_item_id=? AND candidate_set_id=?
                  AND state='waiting' AND selected_ordinal IS NULL AND answered_at IS NULL
                  AND ? BETWEEN minimum_ordinal AND maximum_ordinal""",
            (
                selected_ordinal,
                timestamp,
                new_boundary_user_message_id,
                new_assistant_message_id,
                plan_digest,
                outcome_digest,
                current.question.id,
                identifier,
                current.candidate_set.id,
                selected_ordinal,
            ),
        )
        if question_cursor.rowcount != 1:
            raise WorkItemConflictError("candidate question CAS lost its state race")
        work_cursor = conn.execute(
            """UPDATE work_items
                  SET state='completed',transition='candidate_replayed',
                      revision=revision+1,updated_at=?,closed_at=?
                WHERE id=? AND user_id=? AND conversation_id=?
                  AND kind='select_archive_candidate_and_replay_evidence'
                  AND state='waiting_for_input'
                  AND revision=? AND expires_at>?""",
            (
                timestamp,
                timestamp,
                identifier,
                user,
                conversation,
                revision,
                timestamp,
            ),
        )
        if work_cursor.rowcount != 1:
            raise WorkItemConflictError("candidate selection CAS lost its revision race")
        updated = _fetch_candidate_work_item(
            conn,
            work_item_id=identifier,
            user_id=user,
            conversation_id=conversation,
        )
        if updated is None:  # pragma: no cover - updated in this transaction
            raise WorkItemConflictError("selected candidate Work Item is not durable")
        _validate_stored_anchor(conn, updated, require_latest_message=True)
    except BaseException:
        if savepoint is not None:
            _rollback_work_item_mutation_savepoint(conn, savepoint)
        raise
    if savepoint is not None:
        _release_work_item_mutation_savepoint(conn, savepoint)
    return updated


def promote_archive_candidate_selection_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    selected_evidence_work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    selected_ordinal: int,
    new_boundary_user_message_id: str,
    new_assistant_message_id: str,
    new_accepted_plan_sha256: str,
    new_accepted_outcome_sha256: str,
    now: str | None = None,
) -> tuple[ArchiveCandidateSelectionWorkItem, RecallSelectedArchiveEvidenceWorkItem]:
    """Complete one exact candidate replay and promote its evidence atomically."""

    _require_transaction(conn)
    selected_identifier = _scope(
        selected_evidence_work_item_id,
        _WORK_ITEM_ID_RE,
        label="selected_evidence_work_item_id",
    )
    savepoint = _begin_work_item_mutation_savepoint(conn)
    try:
        completed = _accept_archive_candidate_selection(
            conn,
            work_item_id=work_item_id,
            user_id=user_id,
            conversation_id=conversation_id,
            expected_revision=expected_revision,
            selected_ordinal=selected_ordinal,
            new_boundary_user_message_id=new_boundary_user_message_id,
            new_assistant_message_id=new_assistant_message_id,
            new_accepted_plan_sha256=new_accepted_plan_sha256,
            new_accepted_outcome_sha256=new_accepted_outcome_sha256,
            now=now,
            manage_savepoint=False,
        )
        selected = completed.candidate_set.selected_evidence(
            selected_ordinal,
            work_item_id=selected_identifier,
        )
        frame = RecallSelectedArchiveEvidenceActiveFrame()
        conn.execute(
            """INSERT INTO work_items(
                   id,user_id,conversation_id,kind,goal,state,playbook,
                   completion_contract,active_frame_json,anchor_user_message_id,
                   anchor_assistant_message_id,accepted_plan_sha256,
                   accepted_outcome_sha256,revision,transition,created_at,
                   updated_at,expires_at,closed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
            (
                selected_identifier,
                completed.user_id,
                completed.conversation_id,
                WorkKind.RECALL_SELECTED_ARCHIVE_EVIDENCE.value,
                WorkGoal.EXACT_SELECTED_ARCHIVE_EVIDENCE_RECALL.value,
                WorkState.ACTIVE.value,
                WorkPlaybook.RECALL_SELECTED_ARCHIVE_EVIDENCE.value,
                WorkCompletionContract.ACCEPTED_EXACT_SELECTED_ARCHIVE_EVIDENCE.value,
                frame.to_json(),
                new_boundary_user_message_id,
                new_assistant_message_id,
                completed.question.accepted_replay_plan_sha256,
                completed.question.accepted_replay_outcome_sha256,
                2,
                WorkTransition.EVIDENCE_REPLAYED.value,
                completed.updated_at,
                completed.updated_at,
                _expiry(completed.updated_at),
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
            selected.to_storage_payload(),
        )
        promoted = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=selected_identifier,
            user_id=completed.user_id,
            conversation_id=completed.conversation_id,
        )
        if promoted is None:  # pragma: no cover - inserted in this savepoint
            raise WorkItemConflictError("promoted selected-evidence Work Item is not durable")
    except BaseException as exc:
        _rollback_work_item_mutation_savepoint(conn, savepoint)
        if isinstance(exc, sqlite3.IntegrityError):
            raise WorkItemConflictError("candidate promotion lost its state race") from exc
        raise
    _release_work_item_mutation_savepoint(conn, savepoint)
    return completed, promoted


def suspend_after_replay_failure_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    selected_ordinal: int,
    new_boundary_user_message_id: str,
    new_assistant_message_id: str,
    new_accepted_plan_sha256: str,
    new_accepted_outcome_sha256: str,
    now: str | None = None,
) -> ArchiveCandidateSelectionWorkItem:
    """CAS-suspend after one exact source-free accepted candidate replay failure."""

    _require_transaction(conn)
    revision = _revision(expected_revision)
    identifier = _scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    current = _fetch_candidate_work_item(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if (
        current is None
        or current.state is not WorkState.WAITING_FOR_INPUT
        or current.revision != revision
        or current.question.prompt_revision != revision
        or current.question.has_replay_failure_receipt
    ):
        raise WorkItemConflictError("candidate Work Item revision/state is no longer current")
    timestamp = _logical_now(now, current_updated_at=current.updated_at)
    if current.expires_at <= timestamp:
        raise WorkItemConflictError("candidate Work Item revision/state is no longer current")
    try:
        selected = current.candidate_set.selected_evidence(selected_ordinal)
    except ArchiveCandidateSelectionError as exc:
        raise WorkItemConflictError("candidate ordinal is not in the closed set") from exc
    _validate_stored_anchor(conn, current, require_latest_message=False)
    _validate_immediate_followup(
        conn,
        user_id=user,
        conversation_id=conversation,
        previous_assistant_message_id=current.question.prompt_assistant_message_id,
        boundary_user_message_id=new_boundary_user_message_id,
    )
    _validate_boundary_ordinal(
        conn,
        user_id=user,
        conversation_id=conversation,
        boundary_user_message_id=new_boundary_user_message_id,
        expected_ordinal=selected_ordinal,
    )
    plan_digest = _digest(new_accepted_plan_sha256, label="new_accepted_plan_sha256")
    outcome_digest = _digest(
        new_accepted_outcome_sha256,
        label="new_accepted_outcome_sha256",
    )
    _validate_source_free_replay_failure_publication(
        conn,
        user_id=user,
        conversation_id=conversation,
        boundary_user_message_id=new_boundary_user_message_id,
        assistant_message_id=new_assistant_message_id,
        accepted_plan_sha256=plan_digest,
        accepted_outcome_sha256=outcome_digest,
        evidence=selected,
        require_latest_message=True,
    )
    savepoint = _begin_work_item_mutation_savepoint(conn)
    try:
        question_cursor = conn.execute(
            """UPDATE work_item_archive_candidate_questions
                  SET failed_ordinal=?,failure_boundary_user_message_id=?,
                      failure_assistant_message_id=?,failure_recorded_at=?,
                      accepted_failure_plan_sha256=?,accepted_failure_outcome_sha256=?
                WHERE id=? AND work_item_id=? AND candidate_set_id=?
                  AND state='waiting' AND selected_ordinal IS NULL AND answered_at IS NULL
                  AND failed_ordinal IS NULL AND failure_boundary_user_message_id IS NULL
                  AND failure_assistant_message_id IS NULL AND failure_recorded_at IS NULL
                  AND accepted_failure_plan_sha256 IS NULL
                  AND accepted_failure_outcome_sha256 IS NULL
                  AND prompt_revision=? AND prompt_assistant_message_id=?""",
            (
                selected_ordinal,
                new_boundary_user_message_id,
                new_assistant_message_id,
                timestamp,
                plan_digest,
                outcome_digest,
                current.question.id,
                identifier,
                current.candidate_set.id,
                revision,
                current.question.prompt_assistant_message_id,
            ),
        )
        if question_cursor.rowcount != 1:
            raise WorkItemConflictError("candidate replay failure CAS lost its question race")
        work_cursor = conn.execute(
            """UPDATE work_items
                  SET state='suspended',transition='suspended',revision=revision+1,
                      updated_at=?,closed_at=NULL
                WHERE id=? AND user_id=? AND conversation_id=?
                  AND kind='select_archive_candidate_and_replay_evidence'
                  AND state='waiting_for_input' AND revision=? AND expires_at>?""",
            (
                timestamp,
                identifier,
                user,
                conversation,
                revision,
                timestamp,
            ),
        )
        if work_cursor.rowcount != 1:
            raise WorkItemConflictError("candidate replay failure CAS lost its revision race")
        updated = _fetch_candidate_work_item(
            conn,
            work_item_id=identifier,
            user_id=user,
            conversation_id=conversation,
        )
        if updated is None:  # pragma: no cover - updated in this transaction
            raise WorkItemConflictError("failed candidate replay receipt is not durable")
        _validate_stored_anchor(conn, updated, require_latest_message=True)
    except BaseException:
        _rollback_work_item_mutation_savepoint(conn, savepoint)
        raise
    _release_work_item_mutation_savepoint(conn, savepoint)
    return updated


def _cas_candidate_lifecycle(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    from_states: frozenset[WorkState],
    target_state: WorkState,
    now: str | None,
    require_due: bool,
) -> ArchiveCandidateSelectionWorkItem:
    _require_transaction(conn)
    revision = _revision(expected_revision)
    identifier = _scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    current = _fetch_candidate_work_item(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if current is None or current.state not in from_states or current.revision != revision:
        raise WorkItemConflictError("candidate Work Item revision/state is no longer current")
    timestamp = _logical_now(now, current_updated_at=current.updated_at)
    if (current.expires_at <= timestamp) is not require_due:
        raise WorkItemConflictError("candidate Work Item revision/state is no longer current")
    _validate_stored_anchor(conn, current, require_latest_message=False)
    transition = {
        WorkState.SUSPENDED: WorkTransition.SUSPENDED,
        WorkState.CANCELLED: WorkTransition.CANCELLED,
        WorkState.EXPIRED: WorkTransition.EXPIRED,
    }[target_state]
    states = tuple(sorted(state.value for state in from_states))
    placeholders = ",".join("?" for _item in states)
    due_predicate = "expires_at<=?" if require_due else "expires_at>?"
    cursor = conn.execute(
        f"""UPDATE work_items
               SET state=?,transition=?,revision=revision+1,updated_at=?,closed_at=?
             WHERE id=? AND user_id=? AND conversation_id=?
               AND kind='select_archive_candidate_and_replay_evidence' AND revision=?
               AND state IN ({placeholders}) AND {due_predicate}""",  # nosec B608
        (
            target_state.value,
            transition.value,
            timestamp,
            timestamp if target_state in {WorkState.CANCELLED, WorkState.EXPIRED} else None,
            identifier,
            user,
            conversation,
            revision,
            *states,
            timestamp,
        ),
    )
    if cursor.rowcount != 1:
        raise WorkItemConflictError("candidate lifecycle CAS lost its revision race")
    updated = _fetch_candidate_work_item(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if updated is None:  # pragma: no cover
        raise WorkItemConflictError("candidate lifecycle state is not durable")
    return updated


def cancel_archive_candidate_selection_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    now: str | None = None,
) -> ArchiveCandidateSelectionWorkItem:
    return _cas_candidate_lifecycle(
        conn,
        work_item_id=work_item_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
        from_states=frozenset({WorkState.WAITING_FOR_INPUT, WorkState.SUSPENDED}),
        target_state=WorkState.CANCELLED,
        now=now,
        require_due=False,
    )


def expire_archive_candidate_selection_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    now: str | None = None,
) -> ArchiveCandidateSelectionWorkItem:
    return _cas_candidate_lifecycle(
        conn,
        work_item_id=work_item_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
        from_states=frozenset({WorkState.WAITING_FOR_INPUT, WorkState.SUSPENDED}),
        target_state=WorkState.EXPIRED,
        now=now,
        require_due=True,
    )


def expire_due_archive_candidate_selection_work_items_in_transaction(
    conn: sqlite3.Connection,
    *,
    now: str | None = None,
    user_id: str | None = None,
) -> int:
    _require_transaction(conn)
    timestamp = _now(now)
    scope_sql = ""
    scope_parameters: tuple[object, ...] = ()
    if user_id is not None:
        scope_sql = "user_id=? AND "
        scope_parameters = (_scope(user_id, _USER_ID_RE, label="user_id"),)
    exhausted = conn.execute(
        f"""DELETE FROM work_items
              WHERE {scope_sql}kind='select_archive_candidate_and_replay_evidence'
                AND state IN ('waiting_for_input','suspended') AND expires_at<=?
                AND revision>=2147483647""",  # nosec B608 - closed optional predicate
        (*scope_parameters, timestamp),
    )
    cursor = conn.execute(
        f"""UPDATE work_items
              SET state='expired',transition='expired',revision=revision+1,
                  updated_at=?,closed_at=?
            WHERE {scope_sql}kind='select_archive_candidate_and_replay_evidence'
              AND state IN ('waiting_for_input','suspended') AND expires_at<=?
              AND revision<2147483647""",  # nosec B608 - closed optional predicate
        (timestamp, timestamp, *scope_parameters, timestamp),
    )
    return max(0, int(exhausted.rowcount or 0)) + max(0, int(cursor.rowcount or 0))


__all__ = [
    "accept_archive_candidate_selection_in_transaction",
    "archive_candidate_selection_is_displaced_in_transaction",
    "cancel_archive_candidate_selection_in_transaction",
    "create_archive_candidate_selection_work_item_in_transaction",
    "expire_archive_candidate_selection_in_transaction",
    "expire_due_archive_candidate_selection_work_items_in_transaction",
    "get_archive_candidate_selection_work_item_for_export_in_transaction",
    "get_archive_candidate_selection_work_item_in_transaction",
    "get_current_archive_candidate_selection_work_item_in_transaction",
    "get_waiting_archive_candidate_selection_work_item_in_transaction",
    "new_archive_candidate_question_id",
    "new_archive_candidate_selection_work_item_id",
    "new_archive_candidate_set_id",
    "promote_archive_candidate_selection_in_transaction",
    "reask_archive_candidate_selection_in_transaction",
    "retire_displaced_archive_candidate_selection_in_transaction",
    "suspend_after_replay_failure_in_transaction",
]
