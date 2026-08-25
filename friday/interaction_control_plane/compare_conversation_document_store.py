"""Transaction-local reader/writer for schema-42 conversation/document Work Items."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from friday.interaction_control_plane.archive_candidate_selection import (
    ArchiveCandidateItem,
    ArchiveCandidateSelectionError,
    ArchiveCandidateSet,
    archive_candidate_reask_prompt,
    archive_candidate_selection_offer_suffix,
    parse_archive_candidate_ordinal,
)
from friday.interaction_control_plane.archive_candidate_selection_store import (
    _candidate_offer_is_exact,
    _text_sha256,
    _validate_boundary_ordinal,
)
from friday.interaction_control_plane.archive_evidence_work_item_store import (
    _validate_archive_anchor,
    get_recall_selected_archive_evidence_work_item_in_transaction,
)
from friday.interaction_control_plane.compare_conversation_document import (
    COMPARE_DOCUMENT_CANDIDATE_REASK_VERDICT_KIND,
    COMPARE_DOCUMENT_CANDIDATE_REQUIRED_VERDICT_KIND,
    COMPARE_DOCUMENT_REFERENCE_PROMPT,
    COMPARE_DOCUMENT_REFERENCE_REQUIRED_VERDICT_KIND,
    AcceptedComparisonResultIdentity,
    CompareConversationDocumentActiveFrame,
    CompareConversationWithDocumentWorkItem,
    DocumentReferenceAdmissionShape,
    DocumentReferenceQuestion,
    DocumentReferenceQuestionKind,
    DocumentReferenceQuestionState,
    ResolvedDocumentIdentity,
    ResolvedDocumentProvenance,
    load_accepted_comparison_outcome_receipt,
)
from friday.interaction_control_plane.selected_archive_evidence import (
    SelectedArchiveCorpus,
    SelectedArchiveCoverageGrade,
    SelectedArchiveEvidence,
)
from friday.interaction_control_plane.work_item_contract import (
    COMPARE_CONVERSATION_DOCUMENT_ANSWER_MAX_BYTES,
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
    _release_work_item_mutation_savepoint,
    _rollback_work_item_mutation_savepoint,
)
from friday.orchestration.archive_recall_outcome import (
    ArchiveRecallLane,
    ArchiveRecallOutcome,
    ArchiveRecallOutcomeError,
    ArchiveRecallStatus,
    load_accepted_archive_recall_outcome_receipt,
)
from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval.archive_evidence_snapshot import archive_selected_evidence_snapshot_sha256
from friday.retrieval.archive_search_authority import (
    ArchiveSearchAcceptedCandidateProjection,
    ArchiveSearchCoverageGrade,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    EmbeddingCompatibility,
    EmbeddingIdentity,
    LifecycleRef,
    LifecycleState,
    PassageRef,
    RepresentationKind,
    ResolvedSource,
    RevalidationTarget,
    RevisionKind,
    SourceKind,
    SourceRef,
    SourceRepresentation,
    SourceRevision,
    TextSpanLocator,
)
from friday.retrieval.passage_contract import MessageWindowLocator
from friday.source_identity import raw_source_identity_sha256
from friday.storage._archive_search_documents import PASSAGE_INDEX_VERSION
from friday.storage._intake import (
    select_owned_file_candidate_source_in_transaction,
    select_owned_filename_candidates_in_transaction,
)

_WORK_ITEM_ID_RE = re.compile(r"work_[0-9a-f]{16}\Z")
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}\Z")
_MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{16}\Z")
_USER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}\Z")
_QUESTION_ID_RE = re.compile(r"question_[0-9a-f]{16}\Z")
_CANDIDATE_SET_ID_RE = re.compile(r"cset_[0-9a-f]{16}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_PREPARED_EXACT_CANDIDATES_AUTHORITY = object()


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


def _validate_candidate_reask_chain(
    conn: sqlite3.Connection,
    item: CompareConversationWithDocumentWorkItem,
    *,
    original_prompt_rowid: int,
    stop_before_rowid: int | None = None,
) -> int:
    """Authenticate every source-free invalid-ordinal/re-ask pair after Q2."""

    candidate_set = item.document_candidate_set
    if candidate_set is None:
        raise WorkItemAnchorError("comparison candidate re-ask has no candidate set")
    parameters: tuple[object, ...] = (
        item.user_id,
        item.conversation_id,
        original_prompt_rowid,
    )
    upper = ""
    if stop_before_rowid is not None:
        upper = " AND rowid<?"
        parameters = (*parameters, stop_before_rowid)
    rows = conn.execute(
        f"""SELECT rowid,id,role,content,reply_to,metadata_json
              FROM messages
             WHERE user_id=? AND conversation_id=? AND rowid>?{upper}
             ORDER BY rowid""",  # nosec B608 - closed optional rowid bound
        parameters,
    ).fetchall()
    if len(rows) % 2:
        raise WorkItemAnchorError("comparison candidate re-ask is incomplete or no longer latest")
    expected_prompt = archive_candidate_reask_prompt(len(candidate_set.candidates))
    latest_prompt_rowid = original_prompt_rowid
    for offset in range(0, len(rows), 2):
        boundary, assistant = rows[offset], rows[offset + 1]
        ordinal = parse_archive_candidate_ordinal(boundary[3])
        if (
            boundary[2] != "user"
            or ordinal is not None
            and ordinal <= len(candidate_set.candidates)
            or assistant[2] != "assistant"
            or assistant[3] != expected_prompt
            or assistant[4] != boundary[1]
            or int(boundary[0]) >= int(assistant[0])
        ):
            raise WorkItemAnchorError("comparison candidate re-ask chain is invalid")
        try:
            metadata = json.loads(str(assistant[5] or ""))
        except (TypeError, ValueError, RecursionError) as exc:
            raise WorkItemAnchorError("comparison candidate re-ask metadata is invalid") from exc
        structural = metadata.get("structural") if isinstance(metadata, Mapping) else None
        if not (
            isinstance(structural, Mapping)
            and structural.get("verdict_kind") == COMPARE_DOCUMENT_CANDIDATE_REASK_VERDICT_KIND
            and structural.get("answer_present") is True
            and structural.get("model_spoke") is False
            and isinstance(metadata.get("interaction_trace"), Mapping)
        ):
            raise WorkItemAnchorError("comparison candidate re-ask is not code-owned")
        latest_prompt_rowid = int(assistant[0])
    return latest_prompt_rowid


def _validate_candidate_publication(
    *,
    metadata: object,
    content: object,
    question: DocumentReferenceQuestion,
    candidate_set: ArchiveCandidateSet | None,
) -> bool:
    """Validate the released candidate receipt and identify its code-owned lane."""

    if candidate_set is None:
        raise WorkItemAnchorError("candidate publication has no durable candidate set")
    try:
        decoded_metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkItemAnchorError("candidate publication metadata is invalid") from exc
    if not isinstance(decoded_metadata, Mapping):
        raise WorkItemAnchorError("candidate publication metadata is invalid")
    try:
        receipt = load_accepted_archive_recall_outcome_receipt(metadata)
    except (ArchiveRecallOutcomeError, TypeError, ValueError) as exc:
        raise WorkItemAnchorError("candidate publication search receipt is invalid") from exc
    outcome = receipt.outcome
    if (
        outcome.lane is not ArchiveRecallLane.FEDERATED_SEARCH
        or outcome.status not in {ArchiveRecallStatus.COMPLETE, ArchiveRecallStatus.PARTIAL}
        or outcome.plan_sha256 != question.accepted_search_plan_sha256
        or receipt.outcome_sha256 != question.accepted_search_outcome_sha256
        or not hmac.compare_digest(
            outcome.answer_sha256,
            _text_sha256(content, label="candidate answer"),
        )
        or outcome.evidence_sha256 != candidate_set.evidence_sha256
        or outcome.coverage_sha256 != candidate_set.coverage_sha256
        or outcome.coverage_grade.value != candidate_set.coverage_grade.value
        or outcome.candidate_projection_sha256 != candidate_set.authority_projection_sha256
        or outcome.candidate_count < len(candidate_set.candidates)
        or outcome.selected_evidence is not None
        or not _candidate_offer_is_exact(content, candidate_set)
    ):
        raise WorkItemAnchorError("candidate publication receipt does not match its set")
    structural = decoded_metadata.get("structural")
    return bool(
        isinstance(structural, Mapping)
        and structural.get("verdict_kind") == COMPARE_DOCUMENT_CANDIDATE_REQUIRED_VERDICT_KIND
    )


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
            exact_filename_publication = _validate_candidate_publication(
                metadata=prompt_row[2],
                content=prompt_row[3],
                question=question,
                candidate_set=item.document_candidate_set,
            )
            if exact_filename_publication and (
                conn.execute(
                    """SELECT 1 FROM messages
                        WHERE id=?
                          AND json_extract(metadata_json,'$.structural.verdict_kind')=?
                          AND json_extract(metadata_json,'$.structural.answer_present')=1
                          AND json_extract(metadata_json,'$.structural.model_spoke')=0""",
                    (
                        question.prompt_assistant_message_id,
                        COMPARE_DOCUMENT_CANDIDATE_REQUIRED_VERDICT_KIND,
                    ),
                ).fetchone()
                is None
            ):
                raise WorkItemAnchorError("exact candidate publication is not code-owned")
        if index and question.prompt_boundary_user_message_id != previous_answer:
            raise WorkItemAnchorError("candidate question is not bound to the document reply")
        if question.state is DocumentReferenceQuestionState.ANSWERED:
            if question.answer_user_message_id is None:
                raise WorkItemAnchorError("answered question has no boundary")
            answer_rowid = _message_rowid(
                conn, item=item, message_id=question.answer_user_message_id, role="user"
            )
            if question.kind is DocumentReferenceQuestionKind.SELECT_DOCUMENT_CANDIDATE:
                effective_prompt_rowid = _validate_candidate_reask_chain(
                    conn,
                    item,
                    original_prompt_rowid=prompt_rowid,
                    stop_before_rowid=answer_rowid,
                )
                _require_adjacent(conn, item, effective_prompt_rowid, answer_rowid)
                _validate_boundary_ordinal(
                    conn,
                    user_id=item.user_id,
                    conversation_id=item.conversation_id,
                    boundary_user_message_id=question.answer_user_message_id,
                    expected_ordinal=question.selected_ordinal,
                )
            else:
                _require_adjacent(conn, item, prompt_rowid, answer_rowid)
            previous_answer = question.answer_user_message_id
            last_rowid = answer_rowid
        else:
            previous_answer = None
            last_rowid = (
                _validate_candidate_reask_chain(
                    conn,
                    item,
                    original_prompt_rowid=prompt_rowid,
                )
                if require_latest_message
                and question.kind is DocumentReferenceQuestionKind.SELECT_DOCUMENT_CANDIDATE
                else prompt_rowid
            )
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
    if boundary_rowid >= int(row[0]):
        raise WorkItemAnchorError("comparison result precedes its answer boundary")
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
    effective_prompt_rowid = (
        _validate_candidate_reask_chain(
            conn,
            item,
            original_prompt_rowid=prompt_rowid,
            stop_before_rowid=boundary_rowid,
        )
        if question.kind is DocumentReferenceQuestionKind.SELECT_DOCUMENT_CANDIDATE
        else prompt_rowid
    )
    _require_adjacent(conn, item, effective_prompt_rowid, boundary_rowid)
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
        require_latest_message=(
            boundary_user_message_id is None and item.state is WorkState.WAITING_FOR_INPUT
        ),
    )
    if boundary_user_message_id is not None:
        if item.state is not WorkState.WAITING_FOR_INPUT:
            raise WorkItemAnchorError("only a waiting comparison accepts an answer boundary")
        _validate_pending_answer_boundary(conn, item, boundary_user_message_id)
    return item


def new_compare_conversation_with_document_work_item_id() -> str:
    """Return one opaque ID suitable for a comparison Work Item."""

    return f"work_{uuid.uuid4().hex[:16]}"


def new_compare_document_question_id() -> str:
    """Return one opaque ID suitable for either closed comparison question."""

    return f"question_{uuid.uuid4().hex[:16]}"


def new_compare_document_candidate_set_id() -> str:
    """Return one opaque ID for a frozen ambiguous-document candidate set."""

    return f"cset_{uuid.uuid4().hex[:16]}"


def validate_compare_conversation_document_candidate_reask_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    invalid_boundary_user_message_id: str,
    reask_assistant_message_id: str,
) -> CompareConversationWithDocumentWorkItem:
    """Authenticate one appended invalid-ordinal/re-ask pair without closing Q2."""

    _require_transaction(conn)
    identifier = _scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    revision = _revision(expected_revision)
    boundary = _scope(
        invalid_boundary_user_message_id,
        _MESSAGE_ID_RE,
        label="invalid_boundary_user_message_id",
    )
    assistant = _scope(
        reask_assistant_message_id,
        _MESSAGE_ID_RE,
        label="reask_assistant_message_id",
    )
    item = _fetch(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if (
        item is None
        or item.state is not WorkState.WAITING_FOR_INPUT
        or item.revision != revision
        or item.document_questions[-1].kind is not DocumentReferenceQuestionKind.SELECT_DOCUMENT_CANDIDATE
        or item.document_questions[-1].state is not DocumentReferenceQuestionState.WAITING
    ):
        raise WorkItemConflictError("comparison candidate re-ask admission is no longer current")
    tail = conn.execute(
        """SELECT id,role FROM messages
             WHERE user_id=? AND conversation_id=? ORDER BY rowid DESC LIMIT 2""",
        (user, conversation),
    ).fetchall()
    if [(str(row[0]), str(row[1])) for row in reversed(tail)] != [
        (boundary, "user"),
        (assistant, "assistant"),
    ]:
        raise WorkItemAnchorError("comparison candidate re-ask is no longer latest")
    _validate_stored_item(conn, item, require_latest_message=True)
    return item


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise WorkItemContractError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _revision(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value < WORK_ITEM_MAX_REVISION:
        raise WorkItemContractError("expected_revision is outside the closed limit")
    return value


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


def _validate_selected_followup_question_publication(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
    origin_assistant_message_id: str,
    boundary_user_message_id: str,
    question_assistant_message_id: str,
) -> None:
    cursor = conn.execute(
        """SELECT question.rowid
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
              AND NOT EXISTS (
                  SELECT 1 FROM messages intervening
                   WHERE intervening.user_id=owner.id
                     AND intervening.conversation_id=conversation.id
                     AND intervening.rowid>origin.rowid
                     AND intervening.rowid<boundary.rowid
              )
             JOIN messages question
               ON question.id=? AND question.user_id=owner.id
              AND question.conversation_id=conversation.id AND question.role='assistant'
              AND question.reply_to=boundary.id AND boundary.rowid<question.rowid
              AND NOT EXISTS (
                  SELECT 1 FROM messages intervening
                   WHERE intervening.user_id=owner.id
                     AND intervening.conversation_id=conversation.id
                     AND intervening.rowid>boundary.rowid
                     AND intervening.rowid<question.rowid
              )
            WHERE owner.id=? AND owner.status='active'
              AND question.content=?
              AND json_extract(question.metadata_json,'$.structural.verdict_kind')=?
              AND json_extract(question.metadata_json,'$.structural.answer_present')=1
              AND json_extract(question.metadata_json,'$.structural.model_spoke')=0
              AND NOT EXISTS (
                  SELECT 1 FROM json_each(question.metadata_json) receipt
                   WHERE receipt.key GLOB 'accepted_*_outcome'
              )
            LIMIT 1""",
        (
            conversation_id,
            origin_assistant_message_id,
            boundary_user_message_id,
            question_assistant_message_id,
            user_id,
            COMPARE_DOCUMENT_REFERENCE_PROMPT,
            COMPARE_DOCUMENT_REFERENCE_REQUIRED_VERDICT_KIND,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise WorkItemAnchorError("comparison question publication is not source-free and exact")
    if (
        conn.execute(
            """SELECT 1 FROM messages
                WHERE user_id=? AND conversation_id=? AND rowid>? LIMIT 1""",
            (user_id, conversation_id, int(row[0])),
        ).fetchone()
        is not None
    ):
        raise WorkItemAnchorError("comparison question publication is no longer latest")


def _validate_answer_chain(
    conn: sqlite3.Connection,
    *,
    item: CompareConversationWithDocumentWorkItem,
    boundary_user_message_id: str,
    following_assistant_message_id: str | None = None,
    allow_intervening_before_assistant: bool = False,
) -> tuple[int, int | None]:
    question = item.document_questions[-1]
    boundary = _scope(
        boundary_user_message_id,
        _MESSAGE_ID_RE,
        label="boundary_user_message_id",
    )
    prompt_rowid = _message_rowid(
        conn,
        item=item,
        message_id=question.prompt_assistant_message_id,
        role="assistant",
    )
    boundary_rowid = _message_rowid(conn, item=item, message_id=boundary, role="user")
    effective_prompt_rowid = (
        _validate_candidate_reask_chain(
            conn,
            item,
            original_prompt_rowid=prompt_rowid,
            stop_before_rowid=boundary_rowid,
        )
        if question.kind is DocumentReferenceQuestionKind.SELECT_DOCUMENT_CANDIDATE
        else prompt_rowid
    )
    _require_adjacent(conn, item, effective_prompt_rowid, boundary_rowid)
    if following_assistant_message_id is None:
        last_rowid = boundary_rowid
        assistant_rowid = None
    else:
        assistant = _scope(
            following_assistant_message_id,
            _MESSAGE_ID_RE,
            label="following_assistant_message_id",
        )
        row = conn.execute(
            """SELECT rowid FROM messages
                WHERE id=? AND user_id=? AND conversation_id=? AND role='assistant'
                  AND reply_to=?""",
            (assistant, item.user_id, item.conversation_id, boundary),
        ).fetchone()
        if row is None or not isinstance(row[0], int):
            raise WorkItemAnchorError("comparison answer publication is not owned and exact")
        assistant_rowid = int(row[0])
        if allow_intervening_before_assistant:
            if assistant_rowid <= boundary_rowid:
                raise WorkItemAnchorError("comparison answer publication precedes its boundary")
        else:
            _require_adjacent(conn, item, boundary_rowid, assistant_rowid)
        last_rowid = assistant_rowid
    if (
        conn.execute(
            """SELECT 1 FROM messages
                WHERE user_id=? AND conversation_id=? AND rowid>? LIMIT 1""",
            (item.user_id, item.conversation_id, last_rowid),
        ).fetchone()
        is not None
    ):
        raise WorkItemAnchorError("comparison answer boundary is no longer latest")
    return boundary_rowid, assistant_rowid


def _insert_selected_evidence(
    conn: sqlite3.Connection,
    evidence: SelectedArchiveEvidence,
) -> None:
    conn.execute(
        """INSERT INTO work_item_selected_evidence(
               work_item_id,corpus,source_ref_json,passage_refs_json,
               source_snapshot_sha256,coverage_sha256,coverage_grade,
               origin_boundary_user_message_id
           ) VALUES(:work_item_id,:corpus,:source_ref_json,:passage_refs_json,
                    :source_snapshot_sha256,:coverage_sha256,:coverage_grade,
                    :origin_boundary_user_message_id)""",
        evidence.to_storage_payload(),
    )


def _insert_question(conn: sqlite3.Connection, question: DocumentReferenceQuestion) -> None:
    payload = question.to_payload()
    conn.execute(
        """INSERT INTO work_item_compare_document_questions(
               id,work_item_id,kind,admission_shape,state,created_at,
               prompt_boundary_user_message_id,prompt_assistant_message_id,
               work_revision,candidate_set_id,answered_at,answer_user_message_id,
               selected_ordinal,accepted_search_plan_sha256,
               accepted_search_outcome_sha256,closed_at,close_reason
           ) VALUES(:id,:work_item_id,:kind,:admission_shape,:state,:created_at,
                    :prompt_boundary_user_message_id,:prompt_assistant_message_id,
                    :work_revision,:candidate_set_id,:answered_at,:answer_user_message_id,
                    :selected_ordinal,:accepted_search_plan_sha256,
                    :accepted_search_outcome_sha256,:closed_at,:close_reason)""",
        payload,
    )


def _insert_candidate_set(conn: sqlite3.Connection, candidate_set: ArchiveCandidateSet) -> None:
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
                        :corpus,:source_ref_json,:passage_refs_json,:source_snapshot_sha256)""",
            payload,
        )


def _insert_document_evidence(
    conn: sqlite3.Connection,
    document: ResolvedDocumentIdentity,
) -> None:
    conn.execute(
        """INSERT INTO work_item_compare_document_evidence(
               work_item_id,provenance,source_ref_json,raw_object_id,
               raw_source_identity_sha256,raw_content_sha256,content_sha256,
               candidate_source_snapshot_sha256,origin_boundary_user_message_id,
               resolved_revision,resolved_at,candidate_set_id,selected_ordinal
           ) VALUES(:work_item_id,:provenance,:source_ref_json,:raw_object_id,
                    :raw_source_identity_sha256,:raw_content_sha256,:content_sha256,
                    :candidate_source_snapshot_sha256,:origin_boundary_user_message_id,
                    :resolved_revision,:resolved_at,:candidate_set_id,:selected_ordinal)""",
        document.to_storage_payload(),
    )


def _ensure_current_upload_alias_in_transaction(
    conn: sqlite3.Connection,
    *,
    item: CompareConversationWithDocumentWorkItem,
    document: ResolvedDocumentIdentity,
) -> None:
    """Durably bind an exact current upload for released schema-42 authority.

    API uploads historically kept their immutable Raw ``source_ref`` but did
    not create a ``file_source_aliases`` row, while the released schema-42
    evidence trigger requires that uploader binding.  Mint one narrow,
    code-owned message alias only after the current-turn boundary, Raw
    registration and uploader metadata all agree.  Older runtimes safely
    ignore the extra alias and the released trigger remains byte-for-byte
    unchanged.
    """

    if document.provenance is not ResolvedDocumentProvenance.CURRENT_TURN_ATTACHMENT:
        return
    existing = conn.execute(
        """SELECT 1 FROM file_source_aliases
            WHERE user_id=? AND uploaded_by=? AND raw_object_id=? LIMIT 1""",
        (document.source_ref.tenant_id, item.user_id, document.raw_object_id),
    ).fetchone()
    if existing is not None:
        return
    exact = conn.execute(
        """SELECT 1
             FROM raw_objects raw
             JOIN users uploader ON uploader.id=? AND uploader.status='active'
             JOIN messages boundary
               ON boundary.id=? AND boundary.user_id=?
              AND boundary.conversation_id=? AND boundary.role='user'
            WHERE raw.id=? AND raw.user_id=? AND raw.source='upload'
              AND raw.content_type='file' AND raw.deleted_at IS NULL
              AND json_type(raw.metadata_json,'$.uploaded_by')='text'
              AND json_extract(raw.metadata_json,'$.uploaded_by')=?
              AND json_type(boundary.metadata_json,'$.conversation_uploaded_raw_ids')='array'
              AND json_array_length(boundary.metadata_json,'$.conversation_uploaded_raw_ids')=1
              AND json_extract(boundary.metadata_json,'$.conversation_uploaded_raw_ids[0]')=raw.id
            LIMIT 1""",
        (
            item.user_id,
            document.origin_boundary_user_message_id,
            item.user_id,
            item.conversation_id,
            document.raw_object_id,
            document.source_ref.tenant_id,
            item.user_id,
        ),
    ).fetchone()
    if exact is None:
        raise WorkItemAnchorError("current upload has no exact uploader authority")
    conn.execute(
        """INSERT INTO file_source_aliases(
               user_id,uploaded_by,source_ref,raw_object_id,supplied_filename,created_at
           ) VALUES(?,?,?,?,?,?)""",
        (
            document.source_ref.tenant_id,
            item.user_id,
            f"friday-compare-current:{document.origin_boundary_user_message_id}",
            document.raw_object_id,
            "",
            document.resolved_at,
        ),
    )


def _insert_comparison_result(
    conn: sqlite3.Connection,
    result: AcceptedComparisonResultIdentity,
) -> None:
    conn.execute(
        """INSERT INTO work_item_compare_outcomes(
               work_item_id,answer_boundary_user_message_id,answer_assistant_message_id,
               accepted_plan_sha256,accepted_outcome_sha256,comparison_status,
               message_coverage_grade,document_verification_complete,
               publication_attested,semantic_verified,message_evidence_sha256,
               document_evidence_sha256,evidence_bundle_sha256,model_evidence_sha256,
               completed_revision,completed_at
           ) VALUES(:work_item_id,:answer_boundary_user_message_id,:answer_assistant_message_id,
                    :accepted_plan_sha256,:accepted_outcome_sha256,:comparison_status,
                    :message_coverage_grade,:document_verification_complete,
                    :publication_attested,:semantic_verified,:message_evidence_sha256,
                    :document_evidence_sha256,:evidence_bundle_sha256,:model_evidence_sha256,
                    :completed_revision,:completed_at)""",
        result.to_storage_payload(),
    )


def create_compare_conversation_with_document_from_selected_followup_in_transaction(
    conn: sqlite3.Connection,
    *,
    selected_work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_selected_revision: int,
    prompt_boundary_user_message_id: str,
    prompt_assistant_message_id: str,
    work_item_id: str | None = None,
    question_id: str | None = None,
    now: str | None = None,
) -> CompareConversationWithDocumentWorkItem:
    """Atomically retire one current selected-evidence Work Item and ask Q1."""

    _require_transaction(conn)
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    selected_identifier = _scope(
        selected_work_item_id,
        _WORK_ITEM_ID_RE,
        label="selected_work_item_id",
    )
    selected_revision = _revision(expected_selected_revision)
    identifier = _scope(
        work_item_id or new_compare_conversation_with_document_work_item_id(),
        _WORK_ITEM_ID_RE,
        label="work_item_id",
    )
    question_identifier = _scope(
        question_id or new_compare_document_question_id(),
        _QUESTION_ID_RE,
        label="question_id",
    )
    boundary = _scope(
        prompt_boundary_user_message_id,
        _MESSAGE_ID_RE,
        label="prompt_boundary_user_message_id",
    )
    prompt = _scope(
        prompt_assistant_message_id,
        _MESSAGE_ID_RE,
        label="prompt_assistant_message_id",
    )
    if identifier == selected_identifier:
        raise WorkItemContractError("comparison Work Item must have a fresh identifier")
    selected = get_recall_selected_archive_evidence_work_item_in_transaction(
        conn,
        work_item_id=selected_identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if selected is None or selected.state is not WorkState.ACTIVE or selected.revision != selected_revision:
        raise WorkItemConflictError("selected evidence Work Item is no longer current")
    timestamp = _logical_now(now, current_updated_at=selected.updated_at)
    if selected.expires_at <= timestamp:
        raise WorkItemConflictError("selected evidence Work Item is no longer current")
    _validate_selected_followup_question_publication(
        conn,
        user_id=user,
        conversation_id=conversation,
        origin_assistant_message_id=selected.anchor_assistant_message_id,
        boundary_user_message_id=boundary,
        question_assistant_message_id=prompt,
    )
    evidence = SelectedArchiveEvidence(
        work_item_id=identifier,
        corpus=selected.selected_evidence.corpus,
        source_ref=selected.selected_evidence.source_ref,
        passage_refs=selected.selected_evidence.passage_refs,
        source_snapshot_sha256=selected.selected_evidence.source_snapshot_sha256,
        coverage_sha256=selected.selected_evidence.coverage_sha256,
        coverage_grade=selected.selected_evidence.coverage_grade,
        origin_boundary_user_message_id=selected.anchor_user_message_id,
    )
    if evidence.corpus is not SelectedArchiveCorpus.MESSAGES:
        raise WorkItemAnchorError("comparison follow-up requires selected message evidence")
    question = DocumentReferenceQuestion(
        id=question_identifier,
        work_item_id=identifier,
        kind=DocumentReferenceQuestionKind.PROVIDE_DOCUMENT_REFERENCE,
        admission_shape=DocumentReferenceAdmissionShape.SELECTED_EVIDENCE_FOLLOWUP,
        state=DocumentReferenceQuestionState.WAITING,
        created_at=timestamp,
        prompt_boundary_user_message_id=boundary,
        prompt_assistant_message_id=prompt,
        work_revision=1,
    )
    savepoint = _begin_work_item_mutation_savepoint(conn)
    try:
        retired = conn.execute(
            """UPDATE work_items
                  SET state='suspended',transition='suspended',revision=revision+1,
                      updated_at=?,closed_at=NULL
                WHERE id=? AND user_id=? AND conversation_id=?
                  AND kind='recall_selected_archive_evidence'
                  AND state='active' AND revision=? AND expires_at>?""",
            (
                timestamp,
                selected_identifier,
                user,
                conversation,
                selected_revision,
                timestamp,
            ),
        )
        if retired.rowcount != 1:
            raise WorkItemConflictError("selected evidence retirement lost its revision race")
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
                WorkKind.COMPARE_CONVERSATION_WITH_DOCUMENT.value,
                WorkGoal.COMPARE_EXACT_MESSAGE_EVIDENCE_WITH_DOCUMENT.value,
                WorkState.WAITING_FOR_INPUT.value,
                WorkPlaybook.COMPARE_CONVERSATION_WITH_DOCUMENT.value,
                WorkCompletionContract.ACCEPTED_EXACT_MESSAGE_AND_DOCUMENT_COMPARISON.value,
                CompareConversationDocumentActiveFrame().to_json(),
                selected.anchor_user_message_id,
                selected.anchor_assistant_message_id,
                selected.accepted_plan_sha256,
                selected.accepted_outcome_sha256,
                1,
                WorkTransition.QUESTION_ASKED.value,
                timestamp,
                timestamp,
                _expiry(timestamp),
            ),
        )
        _insert_selected_evidence(conn, evidence)
        _insert_question(conn, question)
        created = _fetch(
            conn,
            work_item_id=identifier,
            user_id=user,
            conversation_id=conversation,
        )
        if created is None:  # pragma: no cover - inserted in this transaction
            raise WorkItemConflictError("created comparison Work Item is not durable")
        _validate_stored_item(conn, created, require_latest_message=True)
    except BaseException as exc:
        _rollback_work_item_mutation_savepoint(conn, savepoint)
        if isinstance(exc, sqlite3.IntegrityError):
            raise WorkItemConflictError("comparison creation lost its state race") from exc
        raise
    _release_work_item_mutation_savepoint(conn, savepoint)
    return created


def resolve_compare_conversation_document_reference_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    boundary_user_message_id: str,
    document_evidence: ResolvedDocumentIdentity,
    now: str | None = None,
) -> CompareConversationWithDocumentWorkItem:
    """Answer Q1 with one exact current or historical Raw document pin."""

    return _resolve_compare_document_in_transaction(
        conn,
        work_item_id=work_item_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
        boundary_user_message_id=boundary_user_message_id,
        selected_ordinal=None,
        document_evidence=document_evidence,
        now=now,
    )


def resolve_compare_conversation_document_candidate_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    boundary_user_message_id: str,
    selected_ordinal: int,
    document_evidence: ResolvedDocumentIdentity,
    now: str | None = None,
) -> CompareConversationWithDocumentWorkItem:
    """Answer Q2 with an exact ordinal and activate its frozen Raw document pin."""

    return _resolve_compare_document_in_transaction(
        conn,
        work_item_id=work_item_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
        boundary_user_message_id=boundary_user_message_id,
        selected_ordinal=selected_ordinal,
        document_evidence=document_evidence,
        now=now,
    )


@dataclass(frozen=True, slots=True)
class _PreparedCompareDocumentFilenameCandidates:
    """Process-owned Q2 material produced from one transaction snapshot."""

    candidate_set: ArchiveCandidateSet
    prompt: str
    outcome: ArchiveRecallOutcome
    _process_authority: object


def _require_compare_candidate_file_authority(
    conn: sqlite3.Connection,
    *,
    authorization: AuthorizationService,
    actor: ActorContext,
    user_id: str,
) -> None:
    if type(authorization) is not AuthorizationService or type(actor) is not ActorContext:
        raise WorkItemContractError("comparison candidate authority is invalid")
    if actor.own_id != user_id or not actor.user_id:
        raise WorkItemAnchorError("comparison candidate actor scope changed")
    principal = conn.execute(
        "SELECT preset_key,status FROM users WHERE id=?",
        (user_id,),
    ).fetchone()
    if principal is None or str(principal["status"] or "") != "active":
        raise WorkItemAnchorError("comparison candidate owner is unavailable")
    fresh_actor = replace(actor, preset_key=str(principal["preset_key"] or "guest"))
    if not authorization.authorize(fresh_actor, "files.read").allowed:
        raise WorkItemAnchorError("comparison candidate file authority was denied")


def _compare_candidate_raw_snapshot(
    raw: Mapping[str, object],
    *,
    tenant_id: str,
    user_id: str,
) -> tuple[ResolvedSource, str]:
    raw_id = str(raw.get("id") or "")
    content_hash = _digest(raw.get("content_hash"), label="candidate Raw content_sha256")
    body = raw.get("_raw_content")
    if not isinstance(body, str) or not body:
        raise WorkItemAnchorError("comparison filename candidate body is unavailable")
    source_ref = SourceRef(
        SourceKind.DOCUMENT,
        AuthorityScope.TENANT_PRINCIPAL,
        tenant_id,
        user_id,
        CanonicalObjectKind.RAW_OBJECT,
        raw_id,
    )
    representation = SourceRepresentation(RepresentationKind.RAW_OBJECT, raw_id)
    revision = SourceRevision(
        representation,
        RevisionKind.RAW_CONTENT_SHA256,
        content_hash,
    )
    return (
        ResolvedSource.create(
            source_ref=source_ref,
            representations=(representation,),
            lifecycle=(LifecycleRef(representation, LifecycleState.ACTIVE),),
            revisions=(revision,),
            revalidation_targets=(RevalidationTarget(representation, AuthorityScope.TENANT_PRINCIPAL),),
        ),
        body,
    )


def _validate_compare_candidate_snapshot(
    selected: SelectedArchiveEvidence,
    *,
    resolved_source: ResolvedSource,
    body: str,
) -> None:
    excerpts: list[str] = []
    for passage in selected.passage_refs:
        locator = passage.locator
        if (
            type(locator) is not TextSpanLocator
            or not passage.revision_matches(resolved_source)
            or locator.start_char < 0
            or locator.end_char > len(body)
        ):
            raise WorkItemAnchorError("comparison filename candidate passage changed")
        excerpt = body[locator.start_char : locator.end_char]
        if not excerpt:
            raise WorkItemAnchorError("comparison filename candidate passage changed")
        excerpts.append(excerpt)
    snapshot = archive_selected_evidence_snapshot_sha256(
        resolved_source,
        selected.passage_refs,
        tuple(excerpts),
    )
    if not hmac.compare_digest(snapshot, selected.source_snapshot_sha256):
        raise WorkItemAnchorError("comparison filename candidate snapshot changed")


def reauthorize_compare_conversation_document_filename_candidate_in_transaction(
    conn: sqlite3.Connection,
    *,
    authorization: AuthorizationService,
    actor: ActorContext,
    item: CompareConversationWithDocumentWorkItem,
    selected_ordinal: int,
) -> SelectedArchiveEvidence:
    """Re-prove one frozen ordinal against current Raw authority and bytes."""

    _require_transaction(conn)
    if type(item) is not CompareConversationWithDocumentWorkItem:
        raise WorkItemContractError("comparison candidate item is invalid")
    _require_compare_candidate_file_authority(
        conn,
        authorization=authorization,
        actor=actor,
        user_id=item.user_id,
    )
    candidate_set = item.document_candidate_set
    if candidate_set is None:
        raise WorkItemAnchorError("comparison candidate set is unavailable")
    try:
        selected = candidate_set.selected_evidence(selected_ordinal, work_item_id=item.id)
    except ArchiveCandidateSelectionError as exc:
        raise WorkItemAnchorError("comparison candidate ordinal is invalid") from exc
    if (
        selected.corpus is not SelectedArchiveCorpus.DOCUMENTS
        or selected.source_ref.tenant_id != actor.user_id
        or selected.source_ref.principal_id != item.user_id
    ):
        raise WorkItemAnchorError("comparison candidate scope changed")
    raw = select_owned_file_candidate_source_in_transaction(
        conn,
        actor.user_id,
        item.user_id,
        selected.source_ref.canonical_object_id,
    )
    if raw is None:
        raise WorkItemAnchorError("comparison candidate Raw is unavailable")
    resolved_source, body = _compare_candidate_raw_snapshot(
        raw,
        tenant_id=actor.user_id,
        user_id=item.user_id,
    )
    if resolved_source.source_ref != selected.source_ref:
        raise WorkItemAnchorError("comparison candidate source changed")
    _validate_compare_candidate_snapshot(
        selected,
        resolved_source=resolved_source,
        body=body,
    )
    return selected


def _candidate_material_sha256(value: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkItemContractError("comparison candidate material is invalid") from exc
    return hashlib.sha256(encoded).hexdigest()


def _safe_candidate_display(value: object, *, fallback: str, maximum: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    return (text or fallback)[:maximum]


def prepare_compare_conversation_document_filename_candidates_in_transaction(
    conn: sqlite3.Connection,
    *,
    authorization: AuthorizationService,
    actor: ActorContext,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    boundary_user_message_id: str,
    filename: str,
    candidate_set_id: str | None = None,
) -> _PreparedCompareDocumentFilenameCandidates:
    """Freeze every exact, readable duplicate filename as one durable Q2 set."""

    _require_transaction(conn)
    revision = _revision(expected_revision)
    if revision != 1:
        raise WorkItemConflictError("only Q1 can advance to a candidate question")
    identifier = _scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    boundary = _scope(
        boundary_user_message_id,
        _MESSAGE_ID_RE,
        label="boundary_user_message_id",
    )
    exact_filename = str(filename or "").strip()
    if not exact_filename or len(exact_filename) > 260:
        raise WorkItemContractError("comparison filename is outside the closed limit")
    _require_compare_candidate_file_authority(
        conn,
        authorization=authorization,
        actor=actor,
        user_id=user,
    )

    current = _fetch(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if (
        current is None
        or current.state is not WorkState.WAITING_FOR_INPUT
        or current.revision != revision
        or len(current.document_questions) != 1
        or current.document_questions[0].kind is not DocumentReferenceQuestionKind.PROVIDE_DOCUMENT_REFERENCE
        or current.document_questions[0].state is not DocumentReferenceQuestionState.WAITING
    ):
        raise WorkItemConflictError("comparison Work Item revision/state is no longer current")
    _validate_stored_item(conn, current, require_latest_message=False)
    _validate_answer_chain(
        conn,
        item=current,
        boundary_user_message_id=boundary,
    )
    selected = select_owned_filename_candidates_in_transaction(
        conn,
        actor.user_id,
        user,
        exact_filename,
        limit=21,
    )
    rows = selected.get("items")
    total = selected.get("total")
    if (
        type(rows) is not list
        or not isinstance(total, int)
        or isinstance(total, bool)
        or selected.get("complete") is not True
        or total != len(rows)
        or not 2 <= total <= 20
    ):
        raise WorkItemAnchorError("comparison filename ambiguity is not a complete closed set")

    candidates: list[ArchiveCandidateItem] = []
    display_rows: list[tuple[str, str, str, str, int]] = []
    for ordinal, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise WorkItemAnchorError("comparison filename candidate row is invalid")
        raw_id = str(row.get("id") or "")
        raw = select_owned_file_candidate_source_in_transaction(
            conn,
            actor.user_id,
            user,
            raw_id,
        )
        if raw is None:
            raise WorkItemAnchorError("comparison filename candidate is no longer readable")
        resolved_source, body = _compare_candidate_raw_snapshot(
            raw,
            tenant_id=actor.user_id,
            user_id=user,
        )
        source_ref = resolved_source.source_ref
        excerpt = body[:720]
        raw_revision = next(
            (
                item
                for item in resolved_source.revisions
                if item.kind is RevisionKind.RAW_CONTENT_SHA256
                and item.representation.kind is RepresentationKind.RAW_OBJECT
            ),
            None,
        )
        if raw_revision is None:
            raise WorkItemAnchorError("comparison filename candidate has no Raw revision")
        passage = PassageRef.from_resolved_source(
            resolved_source,
            source_revision=raw_revision,
            locator=TextSpanLocator(chunk_index=0, start_char=0, end_char=len(excerpt)),
            passage_index_version=PASSAGE_INDEX_VERSION,
            embedding=EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
        )
        snapshot = archive_selected_evidence_snapshot_sha256(
            resolved_source,
            (passage,),
            (excerpt,),
        )
        label = f"A{ordinal}"
        candidates.append(
            ArchiveCandidateItem(
                ordinal=ordinal,
                public_citation_label=label,
                corpus=SelectedArchiveCorpus.DOCUMENTS,
                source_ref=source_ref,
                passage_refs=(passage,),
                source_snapshot_sha256=snapshot,
            )
        )
        file_sha256 = str(row.get("file_sha256") or "").casefold()
        if re.fullmatch(r"[0-9a-f]{64}", file_sha256) is None:
            raise WorkItemAnchorError("comparison filename candidate file digest is invalid")
        size_bytes = row.get("size_bytes")
        if type(size_bytes) is not int or not 0 <= size_bytes <= 2**63 - 1:
            raise WorkItemAnchorError("comparison filename candidate size is invalid")
        display_rows.append(
            (
                label,
                f"D{ordinal:02d}-{file_sha256[:8].upper()}",
                _safe_candidate_display(row.get("filename"), fallback="документ", maximum=180),
                _safe_candidate_display(row.get("received_at"), fallback="дата неизвестна", maximum=40),
                size_bytes,
            )
        )

    evidence_sha256 = _candidate_material_sha256(
        {
            "candidates": [item.to_payload() for item in candidates],
            "schema": "friday.compare-document-exact-filename-evidence.v1",
        }
    )
    coverage_sha256 = _candidate_material_sha256(
        {
            "authority_rechecked": True,
            "complete": True,
            "count": total,
            "schema": "friday.compare-document-exact-filename-coverage.v1",
        }
    )
    set_identifier = _scope(
        candidate_set_id or new_compare_document_candidate_set_id(),
        _CANDIDATE_SET_ID_RE,
        label="candidate_set_id",
    )
    candidate_set = ArchiveCandidateSet.from_code_owned_exact_candidates(
        id=set_identifier,
        work_item_id=identifier,
        origin_boundary_user_message_id=boundary,
        evidence_sha256=evidence_sha256,
        coverage_sha256=coverage_sha256,
        coverage_grade=SelectedArchiveCoverageGrade.COMPLETE,
        candidates=tuple(candidates),
    )
    summary = "Нашёл несколько доступных документов с таким именем:\n" + "\n".join(
        f"{label} [{stable_label}] — {display_name} — {received_at} — {size_bytes} байт"
        for label, stable_label, display_name, received_at, size_bytes in display_rows
    )
    prompt = (
        summary
        + "\n\n"
        + archive_candidate_selection_offer_suffix(
            tuple(item.public_citation_label for item in candidate_set.candidates)
        )
    )
    plan_sha256 = _candidate_material_sha256(
        {
            "candidate_set_id": candidate_set.id,
            "filename_sha256": hashlib.sha256(exact_filename.encode("utf-8")).hexdigest(),
            "schema": "friday.compare-document-exact-filename-plan.v1",
            "work_item_id": identifier,
        }
    )
    outcome = ArchiveRecallOutcome(
        lane=ArchiveRecallLane.FEDERATED_SEARCH,
        status=ArchiveRecallStatus.COMPLETE,
        plan_sha256=plan_sha256,
        evidence_sha256=candidate_set.evidence_sha256,
        coverage_sha256=candidate_set.coverage_sha256,
        coverage_grade=ArchiveSearchCoverageGrade.COMPLETE,
        candidate_count=len(candidate_set.candidates),
        used_citation_labels=tuple(item.public_citation_label for item in candidate_set.candidates),
        selected_evidence=None,
        publication_attested=True,
        semantic_verified=False,
        candidate_projection_sha256=candidate_set.authority_projection_sha256,
        answer_sha256=_text_sha256(prompt, label="candidate answer"),
    )
    return _PreparedCompareDocumentFilenameCandidates(
        candidate_set=candidate_set,
        prompt=prompt,
        outcome=outcome,
        _process_authority=_PREPARED_EXACT_CANDIDATES_AUTHORITY,
    )


def _resolve_compare_document_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    boundary_user_message_id: str,
    selected_ordinal: int | None,
    document_evidence: ResolvedDocumentIdentity,
    now: str | None,
) -> CompareConversationWithDocumentWorkItem:
    _require_transaction(conn)
    revision = _revision(expected_revision)
    identifier = _scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    boundary = _scope(
        boundary_user_message_id,
        _MESSAGE_ID_RE,
        label="boundary_user_message_id",
    )
    if type(document_evidence) is not ResolvedDocumentIdentity:
        raise WorkItemContractError("document_evidence must use the exact typed contract")
    current = _fetch(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if current is None or current.state is not WorkState.WAITING_FOR_INPUT or current.revision != revision:
        raise WorkItemConflictError("comparison Work Item revision/state is no longer current")
    timestamp = _logical_now(now, current_updated_at=current.updated_at)
    if current.expires_at <= timestamp:
        raise WorkItemConflictError("comparison Work Item revision/state is no longer current")
    _validate_stored_item(conn, current, require_latest_message=False)
    _validate_answer_chain(
        conn,
        item=current,
        boundary_user_message_id=boundary,
    )
    question = current.document_questions[-1]
    candidate_mode = question.kind is DocumentReferenceQuestionKind.SELECT_DOCUMENT_CANDIDATE
    if candidate_mode != (selected_ordinal is not None):
        raise WorkItemConflictError("comparison question kind no longer matches the answer")
    if candidate_mode:
        candidate_set = current.document_candidate_set
        parsed = conn.execute("SELECT content FROM messages WHERE id=?", (boundary,)).fetchone()
        ordinal = None if parsed is None else parse_archive_candidate_ordinal(parsed[0])
        if (
            candidate_set is None
            or ordinal != selected_ordinal
            or selected_ordinal is None
            or not 1 <= selected_ordinal <= len(candidate_set.candidates)
        ):
            raise WorkItemAnchorError("comparison candidate ordinal is not exact")
        _validate_boundary_ordinal(
            conn,
            user_id=user,
            conversation_id=conversation,
            boundary_user_message_id=boundary,
            expected_ordinal=selected_ordinal,
        )
    if (
        document_evidence.work_item_id != identifier
        or document_evidence.origin_boundary_user_message_id != boundary
        or document_evidence.resolved_revision != revision + 1
        or document_evidence.resolved_at != timestamp
    ):
        raise WorkItemContractError("resolved document does not match the exact revision boundary")
    if candidate_mode:
        if document_evidence.provenance is not ResolvedDocumentProvenance.HISTORICAL_CANDIDATE_ORDINAL:
            raise WorkItemContractError("ordinal resolution requires candidate provenance")
    elif document_evidence.provenance not in {
        ResolvedDocumentProvenance.CURRENT_TURN_ATTACHMENT,
        ResolvedDocumentProvenance.HISTORICAL_EXACT_REFERENCE,
    }:
        raise WorkItemContractError("Q1 resolution requires exact non-candidate provenance")
    savepoint = _begin_work_item_mutation_savepoint(conn)
    try:
        question_cursor = conn.execute(
            """UPDATE work_item_compare_document_questions
                  SET state='answered',answered_at=?,answer_user_message_id=?,
                      selected_ordinal=?,closed_at=?,close_reason='answered'
                WHERE id=? AND work_item_id=? AND work_revision=?
                  AND state='waiting'""",
            (
                timestamp,
                boundary,
                selected_ordinal,
                timestamp,
                question.id,
                identifier,
                revision,
            ),
        )
        if question_cursor.rowcount != 1:
            raise WorkItemConflictError("comparison question CAS lost its state race")
        _ensure_current_upload_alias_in_transaction(
            conn,
            item=current,
            document=document_evidence,
        )
        _insert_document_evidence(conn, document_evidence)
        work_cursor = conn.execute(
            """UPDATE work_items
                  SET state='active',transition='document_resolved',
                      revision=revision+1,updated_at=?
                WHERE id=? AND user_id=? AND conversation_id=?
                  AND kind='compare_conversation_with_document'
                  AND state='waiting_for_input' AND revision=? AND expires_at>?""",
            (timestamp, identifier, user, conversation, revision, timestamp),
        )
        if work_cursor.rowcount != 1:
            raise WorkItemConflictError("comparison document CAS lost its revision race")
        updated = _fetch(
            conn,
            work_item_id=identifier,
            user_id=user,
            conversation_id=conversation,
        )
        if updated is None:  # pragma: no cover
            raise WorkItemConflictError("resolved comparison Work Item is not durable")
        _validate_stored_item(conn, updated, require_latest_message=False)
    except BaseException as exc:
        _rollback_work_item_mutation_savepoint(conn, savepoint)
        if isinstance(exc, sqlite3.IntegrityError):
            raise WorkItemAnchorError("resolved document authority is not exact") from exc
        raise
    _release_work_item_mutation_savepoint(conn, savepoint)
    return updated


def ask_compare_conversation_document_candidate_question_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    boundary_user_message_id: str,
    candidate_question_assistant_message_id: str,
    accepted_candidate_projection: ArchiveSearchAcceptedCandidateProjection,
    accepted_search_plan_sha256: str,
    accepted_search_outcome_sha256: str,
    candidate_set_id: str | None = None,
    question_id: str | None = None,
    now: str | None = None,
) -> CompareConversationWithDocumentWorkItem:
    """Answer ambiguous Q1 and durably ask ordinal Q2 over one frozen set."""

    _require_transaction(conn)
    revision = _revision(expected_revision)
    if revision != 1:
        raise WorkItemConflictError("only Q1 can advance to a candidate question")
    identifier = _scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    boundary = _scope(
        boundary_user_message_id,
        _MESSAGE_ID_RE,
        label="boundary_user_message_id",
    )
    assistant = _scope(
        candidate_question_assistant_message_id,
        _MESSAGE_ID_RE,
        label="candidate_question_assistant_message_id",
    )
    if type(accepted_candidate_projection) is not ArchiveSearchAcceptedCandidateProjection:
        raise WorkItemContractError("candidate question requires the exact accepted projection")
    plan_digest = _digest(accepted_search_plan_sha256, label="accepted_search_plan_sha256")
    outcome_digest = _digest(
        accepted_search_outcome_sha256,
        label="accepted_search_outcome_sha256",
    )
    set_identifier = _scope(
        candidate_set_id or new_compare_document_candidate_set_id(),
        _CANDIDATE_SET_ID_RE,
        label="candidate_set_id",
    )
    question_identifier = _scope(
        question_id or new_compare_document_question_id(),
        _QUESTION_ID_RE,
        label="question_id",
    )
    current = _fetch(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if (
        current is None
        or current.state is not WorkState.WAITING_FOR_INPUT
        or current.revision != revision
        or len(current.document_questions) != 1
    ):
        raise WorkItemConflictError("comparison Work Item revision/state is no longer current")
    timestamp = _logical_now(now, current_updated_at=current.updated_at)
    if current.expires_at <= timestamp:
        raise WorkItemConflictError("comparison Work Item revision/state is no longer current")
    _validate_stored_item(conn, current, require_latest_message=False)
    _validate_answer_chain(
        conn,
        item=current,
        boundary_user_message_id=boundary,
        following_assistant_message_id=assistant,
    )
    try:
        candidate_set = ArchiveCandidateSet.from_accepted_projection(
            id=set_identifier,
            work_item_id=identifier,
            origin_boundary_user_message_id=boundary,
            projection=accepted_candidate_projection,
        )
    except ArchiveCandidateSelectionError as exc:
        raise WorkItemContractError("comparison candidate projection is invalid") from exc
    if (
        any(
            candidate.corpus is not SelectedArchiveCorpus.DOCUMENTS
            or candidate.source_ref.principal_id != user
            for candidate in candidate_set.candidates
        )
        or len({candidate.source_ref.tenant_id for candidate in candidate_set.candidates}) != 1
    ):
        raise WorkItemAnchorError("comparison candidate set is not one owned document tenant")
    candidate_question = DocumentReferenceQuestion(
        id=question_identifier,
        work_item_id=identifier,
        kind=DocumentReferenceQuestionKind.SELECT_DOCUMENT_CANDIDATE,
        admission_shape=current.document_questions[0].admission_shape,
        state=DocumentReferenceQuestionState.WAITING,
        created_at=timestamp,
        prompt_boundary_user_message_id=boundary,
        prompt_assistant_message_id=assistant,
        work_revision=2,
        candidate_set_id=set_identifier,
        accepted_search_plan_sha256=plan_digest,
        accepted_search_outcome_sha256=outcome_digest,
    )
    candidate_row = conn.execute(
        "SELECT metadata_json,content FROM messages WHERE id=?",
        (assistant,),
    ).fetchone()
    if candidate_row is None:
        raise WorkItemAnchorError("candidate publication is unavailable")
    _validate_candidate_publication(
        metadata=candidate_row[0],
        content=candidate_row[1],
        question=candidate_question,
        candidate_set=candidate_set,
    )
    savepoint = _begin_work_item_mutation_savepoint(conn)
    try:
        first = current.document_questions[0]
        answered = conn.execute(
            """UPDATE work_item_compare_document_questions
                  SET state='answered',answered_at=?,answer_user_message_id=?,
                      closed_at=?,close_reason='answered'
                WHERE id=? AND work_item_id=? AND work_revision=1
                  AND state='waiting'""",
            (timestamp, boundary, timestamp, first.id, identifier),
        )
        if answered.rowcount != 1:
            raise WorkItemConflictError("comparison Q1 CAS lost its state race")
        _insert_candidate_set(conn, candidate_set)
        _insert_question(conn, candidate_question)
        work_cursor = conn.execute(
            """UPDATE work_items
                  SET transition='question_reasked',revision=revision+1,
                      updated_at=?,expires_at=?
                WHERE id=? AND user_id=? AND conversation_id=?
                  AND kind='compare_conversation_with_document'
                  AND state='waiting_for_input' AND revision=1 AND expires_at>?""",
            (
                timestamp,
                _expiry(timestamp),
                identifier,
                user,
                conversation,
                timestamp,
            ),
        )
        if work_cursor.rowcount != 1:
            raise WorkItemConflictError("comparison Q2 CAS lost its revision race")
        updated = _fetch(
            conn,
            work_item_id=identifier,
            user_id=user,
            conversation_id=conversation,
        )
        if updated is None:  # pragma: no cover
            raise WorkItemConflictError("candidate comparison Work Item is not durable")
        _validate_stored_item(conn, updated, require_latest_message=True)
    except BaseException as exc:
        _rollback_work_item_mutation_savepoint(conn, savepoint)
        if isinstance(exc, sqlite3.IntegrityError):
            raise WorkItemConflictError("comparison candidate question lost its state race") from exc
        raise
    _release_work_item_mutation_savepoint(conn, savepoint)
    return updated


def ask_compare_conversation_document_filename_candidate_question_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    boundary_user_message_id: str,
    candidate_question_assistant_message_id: str,
    prepared_candidates: _PreparedCompareDocumentFilenameCandidates,
    question_id: str | None = None,
    now: str | None = None,
) -> CompareConversationWithDocumentWorkItem:
    """CAS-publish code-owned exact-filename ambiguity as durable Q2."""

    _require_transaction(conn)
    revision = _revision(expected_revision)
    if revision != 1:
        raise WorkItemConflictError("only Q1 can advance to a candidate question")
    identifier = _scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    boundary = _scope(
        boundary_user_message_id,
        _MESSAGE_ID_RE,
        label="boundary_user_message_id",
    )
    assistant = _scope(
        candidate_question_assistant_message_id,
        _MESSAGE_ID_RE,
        label="candidate_question_assistant_message_id",
    )
    if (
        type(prepared_candidates) is not _PreparedCompareDocumentFilenameCandidates
        or prepared_candidates._process_authority is not _PREPARED_EXACT_CANDIDATES_AUTHORITY
        or type(prepared_candidates.candidate_set) is not ArchiveCandidateSet
        or type(prepared_candidates.outcome) is not ArchiveRecallOutcome
        or not isinstance(prepared_candidates.prompt, str)
    ):
        raise WorkItemContractError("candidate question requires process-owned exact candidates")
    candidate_set = prepared_candidates.candidate_set
    outcome = prepared_candidates.outcome
    if (
        candidate_set.work_item_id != identifier
        or candidate_set.origin_boundary_user_message_id != boundary
        or outcome.evidence_sha256 != candidate_set.evidence_sha256
        or outcome.coverage_sha256 != candidate_set.coverage_sha256
        or outcome.candidate_projection_sha256 != candidate_set.authority_projection_sha256
        or outcome.candidate_count != len(candidate_set.candidates)
        or not hmac.compare_digest(
            outcome.answer_sha256,
            _text_sha256(prepared_candidates.prompt, label="candidate answer"),
        )
    ):
        raise WorkItemContractError("prepared comparison candidates changed")
    question_identifier = _scope(
        question_id or new_compare_document_question_id(),
        _QUESTION_ID_RE,
        label="question_id",
    )
    current = _fetch(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if (
        current is None
        or current.state is not WorkState.WAITING_FOR_INPUT
        or current.revision != revision
        or len(current.document_questions) != 1
    ):
        raise WorkItemConflictError("comparison Work Item revision/state is no longer current")
    timestamp = _logical_now(now, current_updated_at=current.updated_at)
    if current.expires_at <= timestamp:
        raise WorkItemConflictError("comparison Work Item revision/state is no longer current")
    _validate_stored_item(conn, current, require_latest_message=False)
    _validate_answer_chain(
        conn,
        item=current,
        boundary_user_message_id=boundary,
        following_assistant_message_id=assistant,
    )
    if (
        any(
            candidate.corpus is not SelectedArchiveCorpus.DOCUMENTS
            or candidate.source_ref.principal_id != user
            for candidate in candidate_set.candidates
        )
        or len({candidate.source_ref.tenant_id for candidate in candidate_set.candidates}) != 1
    ):
        raise WorkItemAnchorError("comparison candidate set is not one owned document tenant")
    candidate_question = DocumentReferenceQuestion(
        id=question_identifier,
        work_item_id=identifier,
        kind=DocumentReferenceQuestionKind.SELECT_DOCUMENT_CANDIDATE,
        admission_shape=current.document_questions[0].admission_shape,
        state=DocumentReferenceQuestionState.WAITING,
        created_at=timestamp,
        prompt_boundary_user_message_id=boundary,
        prompt_assistant_message_id=assistant,
        work_revision=2,
        candidate_set_id=candidate_set.id,
        accepted_search_plan_sha256=outcome.plan_sha256,
        accepted_search_outcome_sha256=outcome.canonical_sha256(),
    )
    candidate_row = conn.execute(
        "SELECT metadata_json,content FROM messages WHERE id=?",
        (assistant,),
    ).fetchone()
    if (
        candidate_row is None
        or candidate_row[1] != prepared_candidates.prompt
        or not _validate_candidate_publication(
            metadata=candidate_row[0],
            content=candidate_row[1],
            question=candidate_question,
            candidate_set=candidate_set,
        )
    ):
        raise WorkItemAnchorError("exact candidate publication is unavailable")
    savepoint = _begin_work_item_mutation_savepoint(conn)
    try:
        first = current.document_questions[0]
        answered = conn.execute(
            """UPDATE work_item_compare_document_questions
                  SET state='answered',answered_at=?,answer_user_message_id=?,
                      closed_at=?,close_reason='answered'
                WHERE id=? AND work_item_id=? AND work_revision=1
                  AND state='waiting'""",
            (timestamp, boundary, timestamp, first.id, identifier),
        )
        if answered.rowcount != 1:
            raise WorkItemConflictError("comparison Q1 CAS lost its state race")
        _insert_candidate_set(conn, candidate_set)
        _insert_question(conn, candidate_question)
        work_cursor = conn.execute(
            """UPDATE work_items
                  SET transition='question_reasked',revision=revision+1,
                      updated_at=?,expires_at=?
                WHERE id=? AND user_id=? AND conversation_id=?
                  AND kind='compare_conversation_with_document'
                  AND state='waiting_for_input' AND revision=1 AND expires_at>?""",
            (
                timestamp,
                _expiry(timestamp),
                identifier,
                user,
                conversation,
                timestamp,
            ),
        )
        if work_cursor.rowcount != 1:
            raise WorkItemConflictError("comparison Q2 CAS lost its revision race")
        updated = _fetch(
            conn,
            work_item_id=identifier,
            user_id=user,
            conversation_id=conversation,
        )
        if updated is None:  # pragma: no cover
            raise WorkItemConflictError("candidate comparison Work Item is not durable")
        _validate_stored_item(conn, updated, require_latest_message=True)
    except BaseException as exc:
        _rollback_work_item_mutation_savepoint(conn, savepoint)
        if isinstance(exc, sqlite3.IntegrityError):
            raise WorkItemConflictError("comparison candidate question lost its state race") from exc
        raise
    _release_work_item_mutation_savepoint(conn, savepoint)
    return updated


def complete_compare_conversation_with_document_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    accepted_result: AcceptedComparisonResultIdentity,
    now: str | None = None,
) -> CompareConversationWithDocumentWorkItem:
    """CAS-accept one latest attested assistant/result pair and complete atomically."""

    _require_transaction(conn)
    revision = _revision(expected_revision)
    identifier = _scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    if type(accepted_result) is not AcceptedComparisonResultIdentity:
        raise WorkItemContractError("accepted_result must use the exact typed contract")
    current = _fetch(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if current is None or current.state is not WorkState.ACTIVE or current.revision != revision:
        raise WorkItemConflictError("comparison Work Item revision/state is no longer current")
    timestamp = _logical_now(now, current_updated_at=current.updated_at)
    if current.expires_at <= timestamp:
        raise WorkItemConflictError("comparison Work Item revision/state is no longer current")
    _validate_stored_item(conn, current, require_latest_message=False)
    latest_question = current.document_questions[-1]
    if (
        accepted_result.work_item_id != identifier
        or accepted_result.answer_boundary_user_message_id != latest_question.answer_user_message_id
        or accepted_result.completed_revision != revision + 1
        or accepted_result.completed_at != timestamp
    ):
        raise WorkItemContractError("accepted comparison result does not match its revision boundary")
    _validate_answer_chain(
        conn,
        item=current,
        boundary_user_message_id=accepted_result.answer_boundary_user_message_id,
        following_assistant_message_id=accepted_result.answer_assistant_message_id,
        allow_intervening_before_assistant=True,
    )
    savepoint = _begin_work_item_mutation_savepoint(conn)
    try:
        _insert_comparison_result(conn, accepted_result)
        work_cursor = conn.execute(
            """UPDATE work_items
                  SET state='completed',transition='comparison_published',
                      revision=revision+1,updated_at=?,closed_at=?
                WHERE id=? AND user_id=? AND conversation_id=?
                  AND kind='compare_conversation_with_document'
                  AND state='active' AND revision=? AND expires_at>?""",
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
            raise WorkItemConflictError("comparison completion CAS lost its revision race")
        updated = _fetch(
            conn,
            work_item_id=identifier,
            user_id=user,
            conversation_id=conversation,
        )
        if updated is None:  # pragma: no cover
            raise WorkItemConflictError("completed comparison Work Item is not durable")
        _validate_stored_item(conn, updated, require_latest_message=False)
    except BaseException as exc:
        _rollback_work_item_mutation_savepoint(conn, savepoint)
        if isinstance(exc, sqlite3.IntegrityError):
            raise WorkItemAnchorError("comparison publication receipt is not exact") from exc
        raise
    _release_work_item_mutation_savepoint(conn, savepoint)
    return updated


def _cas_compare_lifecycle(
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
) -> CompareConversationWithDocumentWorkItem:
    _require_transaction(conn)
    revision = _revision(expected_revision)
    identifier = _scope(work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
    user = _scope(user_id, _USER_ID_RE, label="user_id")
    conversation = _scope(conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
    current = _fetch(
        conn,
        work_item_id=identifier,
        user_id=user,
        conversation_id=conversation,
    )
    if current is None or current.state not in from_states or current.revision != revision:
        raise WorkItemConflictError("comparison Work Item revision/state is no longer current")
    timestamp = _logical_now(now, current_updated_at=current.updated_at)
    if (current.expires_at <= timestamp) is not require_due:
        raise WorkItemConflictError("comparison Work Item revision/state is no longer current")
    try:
        _validate_stored_item(
            conn,
            current,
            allow_disabled_owner=True,
            require_latest_message=False,
        )
    except WorkItemAnchorError:
        # Suspension is the fail-closed sink for a live comparison whose
        # selected message or document pin drifted.  Requiring that stale
        # source to authenticate would make the mandated retirement
        # impossible.  The mutation below remains exact-owner/id/revision/state
        # CAS scoped and neither publishes nor retains source-bearing prose.
        if target_state is not WorkState.SUSPENDED:
            raise
    savepoint = _begin_work_item_mutation_savepoint(conn)
    try:
        if current.state is WorkState.WAITING_FOR_INPUT:
            question = current.document_questions[-1]
            question_cursor = conn.execute(
                """UPDATE work_item_compare_document_questions
                      SET state='closed',closed_at=?,close_reason=?
                    WHERE id=? AND work_item_id=? AND work_revision=?
                      AND state='waiting'""",
                (
                    timestamp,
                    target_state.value,
                    question.id,
                    identifier,
                    revision,
                ),
            )
            if question_cursor.rowcount != 1:
                raise WorkItemConflictError("comparison question lifecycle CAS lost its state race")
        states = tuple(sorted(state.value for state in from_states))
        placeholders = ",".join("?" for _item in states)
        due_predicate = "expires_at<=?" if require_due else "expires_at>?"
        cursor = conn.execute(
            f"""UPDATE work_items
                   SET state=?,transition=?,revision=revision+1,updated_at=?,closed_at=?
                 WHERE id=? AND user_id=? AND conversation_id=?
                   AND kind='compare_conversation_with_document' AND revision=?
                   AND state IN ({placeholders}) AND {due_predicate}""",  # nosec B608
            (
                target_state.value,
                target_state.value,
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
            raise WorkItemConflictError("comparison lifecycle CAS lost its revision race")
        updated = _fetch(
            conn,
            work_item_id=identifier,
            user_id=user,
            conversation_id=conversation,
        )
        if updated is None:  # pragma: no cover
            raise WorkItemConflictError("comparison lifecycle state is not durable")
        try:
            _validate_stored_item(
                conn,
                updated,
                allow_disabled_owner=True,
                require_latest_message=False,
            )
        except WorkItemAnchorError:
            if target_state is not WorkState.SUSPENDED:
                raise
    except BaseException:
        _rollback_work_item_mutation_savepoint(conn, savepoint)
        raise
    _release_work_item_mutation_savepoint(conn, savepoint)
    return updated


def suspend_compare_conversation_with_document_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    now: str | None = None,
) -> CompareConversationWithDocumentWorkItem:
    """Suspend without retaining failure prose or a source-bearing outcome."""

    return _cas_compare_lifecycle(
        conn,
        work_item_id=work_item_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
        from_states=frozenset({WorkState.WAITING_FOR_INPUT, WorkState.ACTIVE}),
        target_state=WorkState.SUSPENDED,
        now=now,
        require_due=False,
    )


def cancel_compare_conversation_with_document_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    now: str | None = None,
) -> CompareConversationWithDocumentWorkItem:
    return _cas_compare_lifecycle(
        conn,
        work_item_id=work_item_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
        from_states=frozenset({WorkState.WAITING_FOR_INPUT, WorkState.ACTIVE, WorkState.SUSPENDED}),
        target_state=WorkState.CANCELLED,
        now=now,
        require_due=False,
    )


def expire_compare_conversation_with_document_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    now: str | None = None,
) -> CompareConversationWithDocumentWorkItem:
    return _cas_compare_lifecycle(
        conn,
        work_item_id=work_item_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
        from_states=frozenset({WorkState.WAITING_FOR_INPUT, WorkState.ACTIVE, WorkState.SUSPENDED}),
        target_state=WorkState.EXPIRED,
        now=now,
        require_due=True,
    )


def expire_due_compare_conversation_with_document_work_items_in_transaction(
    conn: sqlite3.Connection,
    *,
    now: str | None = None,
    user_id: str | None = None,
) -> int:
    """Expire due comparison rows while closing each open typed question first."""

    _require_transaction(conn)
    timestamp = _now(now)
    parameters: tuple[object, ...] = (timestamp,)
    scope = ""
    if user_id is not None:
        scope = " AND user_id=?"
        parameters = (timestamp, _scope(user_id, _USER_ID_RE, label="user_id"))
    cursor = conn.execute(
        f"""SELECT id,user_id,conversation_id,revision
              FROM work_items
             WHERE kind='compare_conversation_with_document'
               AND state IN ('waiting_for_input','active','suspended')
               AND expires_at<=?{scope}
             ORDER BY id""",  # nosec B608 - closed optional owner predicate
        parameters,
    )
    rows = tuple(cursor.fetchall())
    count = 0
    for row in rows:
        identifier, owner, conversation, revision = row
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise WorkItemContractError("stored comparison revision is invalid")
        if revision >= WORK_ITEM_MAX_REVISION:
            deleted = conn.execute(
                """DELETE FROM work_items
                    WHERE id=? AND user_id=? AND conversation_id=?
                      AND kind='compare_conversation_with_document'
                      AND state IN ('waiting_for_input','active','suspended')
                      AND revision=? AND expires_at<=?""",
                (identifier, owner, conversation, revision, timestamp),
            )
            count += max(0, int(deleted.rowcount or 0))
            continue
        expire_compare_conversation_with_document_in_transaction(
            conn,
            work_item_id=str(identifier),
            user_id=str(owner),
            conversation_id=str(conversation),
            expected_revision=revision,
            now=timestamp,
        )
        count += 1
    return count


__all__ = [
    "ask_compare_conversation_document_candidate_question_in_transaction",
    "ask_compare_conversation_document_filename_candidate_question_in_transaction",
    "cancel_compare_conversation_with_document_in_transaction",
    "complete_compare_conversation_with_document_in_transaction",
    "create_compare_conversation_with_document_from_selected_followup_in_transaction",
    "expire_compare_conversation_with_document_in_transaction",
    "expire_due_compare_conversation_with_document_work_items_in_transaction",
    "get_compare_conversation_with_document_work_item_for_export_in_transaction",
    "get_compare_conversation_with_document_work_item_in_transaction",
    "get_current_compare_conversation_with_document_work_item_in_transaction",
    "new_compare_conversation_with_document_work_item_id",
    "new_compare_document_candidate_set_id",
    "new_compare_document_question_id",
    "prepare_compare_conversation_document_filename_candidates_in_transaction",
    "reauthorize_compare_conversation_document_filename_candidate_in_transaction",
    "resolve_compare_conversation_document_candidate_in_transaction",
    "resolve_compare_conversation_document_reference_in_transaction",
    "suspend_compare_conversation_with_document_in_transaction",
    "validate_compare_conversation_document_candidate_reask_in_transaction",
]
