"""Strict schema-42 reader for dormant conversation/document Work Items."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime

from friday.interaction_control_plane.archive_candidate_selection import (
    ArchiveCandidateSet,
    parse_archive_candidate_ordinal,
)
from friday.interaction_control_plane.archive_candidate_selection_store import (
    _candidate_offer_is_exact,
    _text_sha256,
    _validate_boundary_ordinal,
)
from friday.interaction_control_plane.archive_evidence_work_item_store import (
    _validate_archive_anchor,
)
from friday.interaction_control_plane.compare_conversation_document import (
    COMPARE_DOCUMENT_REFERENCE_PROMPT,
    COMPARE_DOCUMENT_REFERENCE_REQUIRED_VERDICT_KIND,
    AcceptedComparisonResultIdentity,
    CompareConversationWithDocumentWorkItem,
    DocumentReferenceAdmissionShape,
    DocumentReferenceQuestion,
    DocumentReferenceQuestionKind,
    DocumentReferenceQuestionState,
    ResolvedDocumentIdentity,
    load_accepted_comparison_outcome_receipt,
)
from friday.interaction_control_plane.selected_archive_evidence import (
    SelectedArchiveCorpus,
    SelectedArchiveEvidence,
)
from friday.interaction_control_plane.work_item_contract import (
    COMPARE_CONVERSATION_DOCUMENT_ANSWER_MAX_BYTES,
    WorkItemContractError,
    WorkState,
    canonical_work_item_instant,
)
from friday.interaction_control_plane.work_item_store import WorkItemAnchorError
from friday.orchestration.archive_recall_outcome import (
    ArchiveRecallLane,
    ArchiveRecallOutcomeError,
    ArchiveRecallStatus,
    load_accepted_archive_recall_outcome_receipt,
)
from friday.retrieval.passage_contract import MessageWindowLocator
from friday.source_identity import raw_source_identity_sha256

_WORK_ITEM_ID_RE = re.compile(r"work_[0-9a-f]{16}\Z")
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}\Z")
_MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{16}\Z")
_USER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}\Z")


def _require_transaction(conn: sqlite3.Connection) -> None:
    if type(conn) is not sqlite3.Connection:
        raise TypeError("comparison reader requires a sqlite3 connection")
    if not conn.in_transaction:
        raise RuntimeError("comparison reader requires an existing transaction")


def _scope(value: object, pattern: re.Pattern[str], *, label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise WorkItemContractError(f"{label} is not a valid identifier")
    return value


def _now(value: str | None) -> str:
    candidate = value or datetime.now(UTC).isoformat(timespec="seconds")
    return canonical_work_item_instant(candidate, label="now")


def _row_mapping(cursor: sqlite3.Cursor, row: object) -> dict[str, object]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if isinstance(row, Mapping):
        return {str(key): value for key, value in row.items()}
    if isinstance(row, tuple) and cursor.description is not None:
        return {str(column[0]): value for column, value in zip(cursor.description, row, strict=True)}
    raise WorkItemContractError("comparison query returned an invalid row")


def _optional_evidence(
    conn: sqlite3.Connection, table: str, work_item_id: str
) -> ResolvedDocumentIdentity | None:
    if table != "work_item_compare_document_evidence":
        raise WorkItemContractError("comparison evidence table is invalid")
    cursor = conn.execute(
        "SELECT * FROM work_item_compare_document_evidence WHERE work_item_id=?",
        (work_item_id,),
    )
    row = cursor.fetchone()
    return None if row is None else ResolvedDocumentIdentity.from_storage_row(_row_mapping(cursor, row))


def _candidate_set(conn: sqlite3.Connection, work_item_id: str) -> ArchiveCandidateSet | None:
    set_cursor = conn.execute(
        "SELECT * FROM work_item_archive_candidate_sets WHERE work_item_id=?",
        (work_item_id,),
    )
    set_row = set_cursor.fetchone()
    if set_row is None:
        return None
    item_cursor = conn.execute(
        """SELECT * FROM work_item_archive_candidate_set_items
            WHERE work_item_id=? ORDER BY ordinal""",
        (work_item_id,),
    )
    return ArchiveCandidateSet.from_storage_rows(
        _row_mapping(set_cursor, set_row),
        tuple(_row_mapping(item_cursor, row) for row in item_cursor.fetchall()),
    )


def _fetch(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
) -> CompareConversationWithDocumentWorkItem | None:
    work_cursor = conn.execute(
        """SELECT * FROM work_items
            WHERE id=? AND user_id=? AND conversation_id=?
              AND kind='compare_conversation_with_document'""",
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
    question_cursor = conn.execute(
        """SELECT * FROM work_item_compare_document_questions
            WHERE work_item_id=? ORDER BY work_revision""",
        (work_item_id,),
    )
    question_rows = question_cursor.fetchall()
    result_cursor = conn.execute(
        "SELECT * FROM work_item_compare_outcomes WHERE work_item_id=?",
        (work_item_id,),
    )
    result_row = result_cursor.fetchone()
    if evidence_row is None or not question_rows:
        raise WorkItemContractError("comparison Work Item sidecar is missing")
    return CompareConversationWithDocumentWorkItem.from_storage_rows(
        _row_mapping(work_cursor, work_row),
        SelectedArchiveEvidence.from_storage_row(_row_mapping(evidence_cursor, evidence_row)),
        tuple(
            DocumentReferenceQuestion.from_storage_row(_row_mapping(question_cursor, row))
            for row in question_rows
        ),
        _candidate_set(conn, work_item_id),
        _optional_evidence(conn, "work_item_compare_document_evidence", work_item_id),
        (
            None
            if result_row is None
            else AcceptedComparisonResultIdentity.from_storage_row(_row_mapping(result_cursor, result_row))
        ),
    )


def _validate_selected_message_scope(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    evidence: SelectedArchiveEvidence,
) -> None:
    if evidence.corpus is not SelectedArchiveCorpus.MESSAGES:
        raise WorkItemAnchorError("comparison evidence is not message evidence")
    source_conversation_id = evidence.source_ref.canonical_object_id
    if (
        conn.execute(
            "SELECT 1 FROM conversations WHERE id=? AND user_id=?",
            (source_conversation_id, user_id),
        ).fetchone()
        is None
    ):
        raise WorkItemAnchorError("selected message source is not owned")
    for passage in evidence.passage_refs:
        locator = passage.locator
        if type(locator) is not MessageWindowLocator:
            raise WorkItemAnchorError("selected message locator is invalid")
        rows = conn.execute(
            """SELECT rowid,id FROM messages
                WHERE user_id=? AND conversation_id=? AND id IN (?,?) ORDER BY rowid""",
            (
                user_id,
                source_conversation_id,
                locator.first_message_id,
                locator.last_message_id,
            ),
        ).fetchall()
        first = next((int(row[0]) for row in rows if str(row[1]) == locator.first_message_id), None)
        last = next((int(row[0]) for row in rows if str(row[1]) == locator.last_message_id), None)
        if first is None or last is None or first > last:
            raise WorkItemAnchorError("selected message boundaries are not exact")
        count = conn.execute(
            """SELECT COUNT(*) FROM messages
                WHERE user_id=? AND conversation_id=? AND rowid BETWEEN ? AND ?""",
            (user_id, source_conversation_id, first, last),
        ).fetchone()
        if count is None or int(count[0]) != locator.context_before + locator.context_after + 1:
            raise WorkItemAnchorError("selected message window cardinality changed")


def _message_rowid(
    conn: sqlite3.Connection,
    *,
    item: CompareConversationWithDocumentWorkItem,
    message_id: str,
    role: str,
) -> int:
    row = conn.execute(
        """SELECT rowid FROM messages
            WHERE id=? AND user_id=? AND conversation_id=? AND role=?""",
        (message_id, item.user_id, item.conversation_id, role),
    ).fetchone()
    if row is None or not isinstance(row[0], int):
        raise WorkItemAnchorError("comparison message anchor is not owned")
    return int(row[0])


def _require_adjacent(
    conn: sqlite3.Connection, item: CompareConversationWithDocumentWorkItem, first: int, second: int
) -> None:
    if (
        first >= second
        or conn.execute(
            """SELECT 1 FROM messages WHERE user_id=? AND conversation_id=?
            AND rowid>? AND rowid<? LIMIT 1""",
            (item.user_id, item.conversation_id, first, second),
        ).fetchone()
        is not None
    ):
        raise WorkItemAnchorError("comparison message anchors are not adjacent")


def _validate_candidate_publication(
    *,
    metadata: object,
    content: object,
    question: DocumentReferenceQuestion,
    candidate_set: ArchiveCandidateSet | None,
) -> None:
    if candidate_set is None:
        raise WorkItemAnchorError("candidate publication has no durable candidate set")
    try:
        receipt = load_accepted_archive_recall_outcome_receipt(metadata)
    except (ArchiveRecallOutcomeError, TypeError, ValueError) as exc:
        raise WorkItemAnchorError("candidate publication has no accepted search receipt") from exc
    outcome = receipt.outcome
    if (
        outcome.lane is not ArchiveRecallLane.FEDERATED_SEARCH
        or outcome.status not in {ArchiveRecallStatus.COMPLETE, ArchiveRecallStatus.PARTIAL}
        or outcome.plan_sha256 != question.accepted_search_plan_sha256
        or receipt.outcome_sha256 != question.accepted_search_outcome_sha256
        or not hmac.compare_digest(outcome.answer_sha256, _text_sha256(content, label="candidate answer"))
        or outcome.evidence_sha256 != candidate_set.evidence_sha256
        or outcome.coverage_sha256 != candidate_set.coverage_sha256
        or outcome.coverage_grade.value != candidate_set.coverage_grade.value
        or outcome.candidate_projection_sha256 != candidate_set.authority_projection_sha256
        or outcome.candidate_count < len(candidate_set.candidates)
        or outcome.selected_evidence is not None
        or not _candidate_offer_is_exact(content, candidate_set)
    ):
        raise WorkItemAnchorError("candidate publication receipt does not match its set")


def _validate_question_anchors(
    conn: sqlite3.Connection,
    item: CompareConversationWithDocumentWorkItem,
    *,
    allow_disabled_owner: bool,
    require_latest_message: bool,
) -> None:
    owner = conn.execute(
        """SELECT status FROM users WHERE id=? AND status IN ('active','disabled')""",
        (item.user_id,),
    ).fetchone()
    if owner is None or (not allow_disabled_owner and str(owner[0]) != "active"):
        raise WorkItemAnchorError("comparison owner is unavailable")
    first = item.document_questions[0]
    if first.admission_shape is DocumentReferenceAdmissionShape.DIRECT_COMPOUND:
        if (
            first.prompt_boundary_user_message_id != item.anchor_user_message_id
            or first.prompt_assistant_message_id != item.anchor_assistant_message_id
        ):
            raise WorkItemAnchorError("direct comparison admission anchors changed")
    else:
        origin_rowid = _message_rowid(
            conn, item=item, message_id=item.anchor_assistant_message_id, role="assistant"
        )
        boundary_rowid = _message_rowid(
            conn,
            item=item,
            message_id=first.prompt_boundary_user_message_id,
            role="user",
        )
        _require_adjacent(conn, item, origin_rowid, boundary_rowid)

    last_rowid = 0
    previous_answer: str | None = None
    for index, question in enumerate(item.document_questions):
        if question.admission_shape is not first.admission_shape:
            raise WorkItemAnchorError("comparison admission shape changed")
        boundary_rowid = _message_rowid(
            conn,
            item=item,
            message_id=question.prompt_boundary_user_message_id,
            role="user",
        )
        prompt_row = conn.execute(
            """SELECT rowid,reply_to,metadata_json,content FROM messages
                WHERE id=? AND user_id=? AND conversation_id=? AND role='assistant'""",
            (
                question.prompt_assistant_message_id,
                item.user_id,
                item.conversation_id,
            ),
        ).fetchone()
        if prompt_row is None or prompt_row[1] != question.prompt_boundary_user_message_id:
            raise WorkItemAnchorError("comparison question reply anchor is invalid")
        prompt_rowid = int(prompt_row[0])
        _require_adjacent(conn, item, boundary_rowid, prompt_rowid)
        if question.kind is DocumentReferenceQuestionKind.PROVIDE_DOCUMENT_REFERENCE:
            structural = conn.execute(
                """SELECT 1 FROM messages
                    WHERE id=?
                      AND json_extract(metadata_json,'$.structural.verdict_kind')=?
                      AND json_extract(metadata_json,'$.structural.answer_present')=1
                      AND json_extract(metadata_json,'$.structural.model_spoke')=0
                      AND (?='direct_compound'
                           OR (content=? AND NOT EXISTS (
                               SELECT 1 FROM json_each(messages.metadata_json) receipt
                                WHERE receipt.key GLOB 'accepted_*_outcome'
                           )))""",
                (
                    question.prompt_assistant_message_id,
                    COMPARE_DOCUMENT_REFERENCE_REQUIRED_VERDICT_KIND,
                    question.admission_shape.value,
                    COMPARE_DOCUMENT_REFERENCE_PROMPT,
                ),
            ).fetchone()
            if structural is None:
                raise WorkItemAnchorError("document-reference publication is not source-free and exact")
        else:
            _validate_candidate_publication(
                metadata=prompt_row[2],
                content=prompt_row[3],
                question=question,
                candidate_set=item.document_candidate_set,
            )
        if index and question.prompt_boundary_user_message_id != previous_answer:
            raise WorkItemAnchorError("candidate question is not bound to the document reply")
        if question.state is DocumentReferenceQuestionState.ANSWERED:
            if question.answer_user_message_id is None:
                raise WorkItemAnchorError("answered question has no boundary")
            answer_rowid = _message_rowid(
                conn, item=item, message_id=question.answer_user_message_id, role="user"
            )
            _require_adjacent(conn, item, prompt_rowid, answer_rowid)
            if question.kind is DocumentReferenceQuestionKind.SELECT_DOCUMENT_CANDIDATE:
                _validate_boundary_ordinal(
                    conn,
                    user_id=item.user_id,
                    conversation_id=item.conversation_id,
                    boundary_user_message_id=question.answer_user_message_id,
                    expected_ordinal=question.selected_ordinal,
                )
            previous_answer = question.answer_user_message_id
            last_rowid = answer_rowid
        else:
            previous_answer = None
            last_rowid = prompt_rowid
    if (
        require_latest_message
        and conn.execute(
            """SELECT 1 FROM messages WHERE user_id=? AND conversation_id=? AND rowid>? LIMIT 1""",
            (item.user_id, item.conversation_id, last_rowid),
        ).fetchone()
        is not None
    ):
        raise WorkItemAnchorError("pending comparison question is no longer latest")


def _validate_result_anchor(conn: sqlite3.Connection, item: CompareConversationWithDocumentWorkItem) -> None:
    result = item.accepted_comparison
    if result is None:
        return
    boundary_rowid = _message_rowid(
        conn,
        item=item,
        message_id=result.answer_boundary_user_message_id,
        role="user",
    )
    latest_question = item.document_questions[-1]
    if result.answer_boundary_user_message_id != latest_question.answer_user_message_id:
        raise WorkItemAnchorError("comparison result is not bound to the resolved document reply")
    cursor = conn.execute(
        """SELECT rowid,reply_to,content,metadata_json FROM messages
            WHERE id=? AND user_id=? AND conversation_id=? AND role='assistant'""",
        (result.answer_assistant_message_id, item.user_id, item.conversation_id),
    )
    row = cursor.fetchone()
    if row is None or row[1] != result.answer_boundary_user_message_id:
        raise WorkItemAnchorError("comparison result publication anchor is invalid")
    _require_adjacent(conn, item, boundary_rowid, int(row[0]))
    try:
        receipt = load_accepted_comparison_outcome_receipt(row[3])
        answer = row[2]
        if not isinstance(answer, str):
            raise WorkItemContractError("comparison answer is not text")
        encoded_answer = answer.encode("utf-8", errors="strict")
        if not encoded_answer or len(encoded_answer) > COMPARE_CONVERSATION_DOCUMENT_ANSWER_MAX_BYTES:
            raise WorkItemContractError("comparison answer exceeds its closed limit")
    except (UnicodeEncodeError, WorkItemContractError) as exc:
        raise WorkItemAnchorError("comparison result receipt is invalid") from exc
    outcome = receipt.outcome
    if (
        conn.execute(
            """SELECT 1 FROM messages WHERE id=?
                AND json_extract(metadata_json,'$.structural.answer_present')=1
                AND json_extract(metadata_json,'$.structural.model_spoke')=1""",
            (result.answer_assistant_message_id,),
        ).fetchone()
        is None
        or not hmac.compare_digest(receipt.outcome_sha256, result.accepted_outcome_sha256)
        or not hmac.compare_digest(outcome.plan_sha256, result.accepted_plan_sha256)
        or not hmac.compare_digest(outcome.answer_sha256, hashlib.sha256(encoded_answer).hexdigest())
        or outcome.status is not result.comparison_status
        or outcome.message_coverage_grade is not result.message_coverage_grade
        or outcome.document_verification_complete is not result.document_verification_complete
        or outcome.publication_attested is not result.publication_attested
        or outcome.semantic_verified is not result.semantic_verified
        or outcome.message_evidence_sha256 != result.message_evidence_sha256
        or outcome.document_evidence_sha256 != result.document_evidence_sha256
        or outcome.evidence_bundle_sha256 != result.evidence_bundle_sha256
        or outcome.model_evidence_sha256 != result.model_evidence_sha256
    ):
        raise WorkItemAnchorError("comparison result receipt changed")


def _validate_resolved_document(
    conn: sqlite3.Connection, item: CompareConversationWithDocumentWorkItem
) -> None:
    document = item.resolved_document_evidence
    if document is None:
        return
    cursor = conn.execute(
        """SELECT id,source,source_ref,content_type,received_at,content_hash,
                  raw_content AS _raw_content,metadata_json AS _raw_metadata
             FROM raw_objects
            WHERE id=? AND user_id=? AND deleted_at IS NULL""",
        (document.raw_object_id, document.source_ref.tenant_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise WorkItemAnchorError("resolved document Raw row is unavailable")
    raw = _row_mapping(cursor, row)
    if raw.get("content_hash") != document.raw_content_sha256 or not hmac.compare_digest(
        raw_source_identity_sha256(raw), document.raw_source_identity_sha256
    ):
        raise WorkItemAnchorError("resolved document Raw identity changed")
    try:
        metadata = json.loads(str(raw.get("_raw_metadata") or ""))
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkItemAnchorError("resolved document registration is invalid") from exc
    if not isinstance(metadata, dict) or metadata.get("sha256") != document.content_sha256:
        raise WorkItemAnchorError("resolved document content registration changed")
    if (
        conn.execute(
            """SELECT 1 FROM file_source_aliases
            WHERE user_id=? AND uploaded_by=? AND raw_object_id=? LIMIT 1""",
            (document.source_ref.tenant_id, item.user_id, document.raw_object_id),
        ).fetchone()
        is None
    ):
        raise WorkItemAnchorError("resolved document uploader authority changed")
    attachment_key = {
        "current_turn_attachment": "conversation_uploaded_raw_ids",
        "historical_exact_reference": "conversation_attachment_raw_ids",
    }.get(document.provenance.value)
    if attachment_key is not None and (
        conn.execute(
            """SELECT 1 FROM messages boundary
                WHERE boundary.id=? AND boundary.user_id=? AND boundary.conversation_id=?
                  AND boundary.role='user'
                  AND json_type(boundary.metadata_json,?)='array'
                  AND json_array_length(boundary.metadata_json,?)=1
                  AND json_extract(boundary.metadata_json,?)=?
                LIMIT 1""",
            (
                document.origin_boundary_user_message_id,
                item.user_id,
                item.conversation_id,
                f"$.{attachment_key}",
                f"$.{attachment_key}",
                f"$.{attachment_key}[0]",
                document.raw_object_id,
            ),
        ).fetchone()
        is None
    ):
        raise WorkItemAnchorError("resolved document is absent from its exact message attachment set")


def _validate_pending_answer_boundary(
    conn: sqlite3.Connection,
    item: CompareConversationWithDocumentWorkItem,
    boundary_user_message_id: str,
) -> None:
    question = item.document_questions[-1]
    if question.state is not DocumentReferenceQuestionState.WAITING:
        raise WorkItemAnchorError("comparison has no pending document question")
    prompt_rowid = _message_rowid(
        conn,
        item=item,
        message_id=question.prompt_assistant_message_id,
        role="assistant",
    )
    boundary = _scope(boundary_user_message_id, _MESSAGE_ID_RE, label="boundary_user_message_id")
    boundary_rowid = _message_rowid(conn, item=item, message_id=boundary, role="user")
    _require_adjacent(conn, item, prompt_rowid, boundary_rowid)
    if (
        conn.execute(
            """SELECT 1 FROM messages
                WHERE user_id=? AND conversation_id=? AND rowid>? LIMIT 1""",
            (item.user_id, item.conversation_id, boundary_rowid),
        ).fetchone()
        is not None
    ):
        raise WorkItemAnchorError("comparison answer boundary is no longer latest")
    if question.kind is DocumentReferenceQuestionKind.SELECT_DOCUMENT_CANDIDATE:
        row = conn.execute("SELECT content FROM messages WHERE id=?", (boundary,)).fetchone()
        parsed = None if row is None else parse_archive_candidate_ordinal(row[0])
        candidate_set = item.document_candidate_set
        if candidate_set is None or parsed is None or parsed > len(candidate_set.candidates):
            raise WorkItemAnchorError("comparison answer is not a valid candidate ordinal")
        _validate_boundary_ordinal(
            conn,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            boundary_user_message_id=boundary,
            expected_ordinal=parsed,
        )


def _validate_stored_item(
    conn: sqlite3.Connection,
    item: CompareConversationWithDocumentWorkItem,
    *,
    allow_disabled_owner: bool = False,
    require_latest_message: bool,
) -> None:
    _validate_archive_anchor(
        conn,
        user_id=item.user_id,
        conversation_id=item.conversation_id,
        boundary_user_message_id=item.anchor_user_message_id,
        assistant_message_id=item.anchor_assistant_message_id,
        accepted_plan_sha256=item.accepted_plan_sha256,
        accepted_outcome_sha256=item.accepted_outcome_sha256,
        evidence=item.selected_message_evidence,
        expected_lane=None,
        expected_source_bearing=True,
        require_latest_message=False,
        allow_disabled_owner=allow_disabled_owner,
    )
    _validate_selected_message_scope(conn, user_id=item.user_id, evidence=item.selected_message_evidence)
    _validate_question_anchors(
        conn,
        item,
        allow_disabled_owner=allow_disabled_owner,
        require_latest_message=require_latest_message,
    )
    _validate_resolved_document(conn, item)
    _validate_result_anchor(conn, item)


def get_compare_conversation_with_document_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
) -> CompareConversationWithDocumentWorkItem | None:
    """Read and authenticate any schema-42 comparison lifecycle state."""

    _require_transaction(conn)
    item = _fetch(
        conn,
        work_item_id=_scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id"),
        user_id=_scope(user_id, _USER_ID_RE, label="user_id"),
        conversation_id=_scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id"),
    )
    if item is not None:
        _validate_stored_item(conn, item, require_latest_message=False)
    return item


def get_compare_conversation_with_document_work_item_for_export_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
) -> CompareConversationWithDocumentWorkItem | None:
    _require_transaction(conn)
    item = _fetch(
        conn,
        work_item_id=_scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id"),
        user_id=_scope(user_id, _USER_ID_RE, label="user_id"),
        conversation_id=_scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id"),
    )
    if item is not None:
        _validate_stored_item(conn, item, allow_disabled_owner=True, require_latest_message=False)
    return item


def get_current_compare_conversation_with_document_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    boundary_user_message_id: str | None = None,
    now: str | None = None,
) -> CompareConversationWithDocumentWorkItem | None:
    """Restart-safe read of the one live comparison journey, if present."""

    _require_transaction(conn)
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    row = conn.execute(
        """SELECT id FROM work_items
            WHERE user_id=? AND conversation_id=?
              AND kind='compare_conversation_with_document'
              AND state IN ('waiting_for_input','active')
              AND expires_at>? LIMIT 1""",
        (user, conversation, _now(now)),
    ).fetchone()
    if row is None:
        return None
    item = _fetch(
        conn,
        work_item_id=_scope(row[0], _WORK_ITEM_ID_RE, label="work_item_id"),
        user_id=user,
        conversation_id=conversation,
    )
    if item is None:  # pragma: no cover - selected in this transaction
        return None
    _validate_stored_item(
        conn,
        item,
        require_latest_message=boundary_user_message_id is None,
    )
    if boundary_user_message_id is not None:
        if item.state is not WorkState.WAITING_FOR_INPUT:
            raise WorkItemAnchorError("only a waiting comparison accepts an answer boundary")
        _validate_pending_answer_boundary(conn, item, boundary_user_message_id)
    return item


__all__ = [
    "get_compare_conversation_with_document_work_item_for_export_in_transaction",
    "get_compare_conversation_with_document_work_item_in_transaction",
    "get_current_compare_conversation_with_document_work_item_in_transaction",
]
