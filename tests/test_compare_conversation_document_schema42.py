from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from friday.account_deletion import (
    _mark_account_deletion_history_clean,
    preflight_account_deletion,
)
from friday.interaction_control_plane.archive_candidate_selection import (
    ArchiveCandidateSet,
    archive_candidate_selection_offer_suffix,
)
from friday.interaction_control_plane.archive_evidence_work_item_store import (
    create_recall_selected_archive_evidence_work_item_in_transaction,
)
from friday.interaction_control_plane.compare_conversation_document import (
    COMPARE_DOCUMENT_REFERENCE_PROMPT,
    COMPARE_DOCUMENT_REFERENCE_REQUIRED_VERDICT_KIND,
    AcceptedComparisonResultIdentity,
    CompareConversationDocumentOutcome,
    CompareConversationDocumentStatus,
    DocumentReferenceAdmissionShape,
    DocumentReferenceQuestionState,
    ResolvedDocumentIdentity,
    ResolvedDocumentProvenance,
    attach_accepted_comparison_outcome_receipt,
    comparison_evidence_bundle_sha256,
    selected_evidence_sha256,
)
from friday.interaction_control_plane.compare_conversation_document_store import (
    ask_compare_conversation_document_candidate_question_in_transaction,
    cancel_compare_conversation_with_document_in_transaction,
    complete_compare_conversation_with_document_in_transaction,
    create_compare_conversation_with_document_from_selected_followup_in_transaction,
    expire_compare_conversation_with_document_in_transaction,
    expire_due_compare_conversation_with_document_work_items_in_transaction,
    get_compare_conversation_with_document_work_item_in_transaction,
    get_current_compare_conversation_with_document_work_item_in_transaction,
    new_compare_conversation_with_document_work_item_id,
    new_compare_document_question_id,
    resolve_compare_conversation_document_candidate_in_transaction,
    resolve_compare_conversation_document_reference_in_transaction,
    suspend_compare_conversation_with_document_in_transaction,
)
from friday.interaction_control_plane.selected_archive_evidence import (
    SelectedArchiveCorpus,
    SelectedArchiveCoverageGrade,
    SelectedArchiveEvidence,
)
from friday.interaction_control_plane.work_item_contract import (
    COMPARE_CONVERSATION_DOCUMENT_ACTIVE_FRAME_JSON,
    WorkItemContractError,
    WorkState,
    WorkTransition,
)
from friday.interaction_control_plane.work_item_schema import validate_work_item_schema
from friday.interaction_control_plane.work_item_store import (
    WorkItemAnchorError,
    WorkItemConflictError,
)
from friday.orchestration.archive_recall_outcome import (
    ArchiveRecallLane,
    ArchiveRecallOutcome,
    ArchiveRecallStatus,
    attach_accepted_archive_recall_outcome_receipt,
)
from friday.retrieval.archive_search_authority import (
    ArchiveSearchAcceptedCandidateProjection,
    ArchiveSearchCandidateProjectionEntry,
    ArchiveSearchCoverageGrade,
    ArchiveSearchSelectedEvidence,
    _new_accepted_candidate_projection,
)
from friday.retrieval.archive_search_contract import ArchiveSearchCorpus
from friday.retrieval.archive_search_message_adapter import MESSAGE_PASSAGE_INDEX_VERSION
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    EmbeddingCompatibility,
    EmbeddingIdentity,
    MessageWindowLocator,
    PassageRef,
    RepresentationKind,
    RevisionKind,
    SourceKind,
    SourceRef,
    SourceRepresentation,
    SourceRevision,
    TextSpanLocator,
)
from friday.source_identity import raw_source_identity_sha256
from friday.storage._archive_search_documents import PASSAGE_INDEX_VERSION
from friday.storage.models import RawObject, new_id

_NOW = "2026-08-25T08:00:00+00:00"
_EXPIRES = "2026-08-25T20:00:00+00:00"
_WORK_ID = "work_4242424242424242"
_QUESTION_ID = "question_4242424242424242"
_ANSWERED_AT = "2026-08-25T08:05:00+00:00"
_RESOLVED_AT = "2026-08-25T08:06:00+00:00"
_COMPLETED_AT = "2026-08-25T08:07:00+00:00"
_CANDIDATE_SET_ID = "cset_4242424242424242"
_SECOND_QUESTION_ID = "question_4343434343434343"
_CANDIDATE_ANSWERED_AT = "2026-08-25T08:10:00+00:00"
_CANDIDATE_RESOLVED_AT = "2026-08-25T08:11:00+00:00"
_CANDIDATE_COMPLETED_AT = "2026-08-25T08:12:00+00:00"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _selected_messages(
    storage: Any,
    *,
    owner: str,
    work_item_id: str,
    origin_boundary_id: str,
) -> tuple[SelectedArchiveEvidence, ArchiveSearchSelectedEvidence]:
    source_conversation = storage.create_conversation(owner, "private source title")
    source_message = storage.store_message(
        source_conversation["id"],
        owner,
        "user",
        "PRIVATE-MESSAGE-BODY-CANARY",
    )
    source = SourceRef(
        SourceKind.CONVERSATION,
        AuthorityScope.PRINCIPAL,
        None,
        owner,
        CanonicalObjectKind.CONVERSATION,
        source_conversation["id"],
    )
    revision = SourceRevision(
        SourceRepresentation(RepresentationKind.CONVERSATION, source_conversation["id"]),
        RevisionKind.MESSAGE_LEDGER_SHA256,
        "1" * 64,
    )
    passage = PassageRef(
        source,
        revision,
        MessageWindowLocator(
            first_message_id=source_message["id"],
            last_message_id=source_message["id"],
            start_at="2026-08-24T00:00:00+00:00",
            end_at="2026-08-24T01:00:00+00:00",
            context_before=0,
            context_after=0,
        ),
        MESSAGE_PASSAGE_INDEX_VERSION,
        EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
    )
    evidence = SelectedArchiveEvidence(
        work_item_id=work_item_id,
        corpus=SelectedArchiveCorpus.MESSAGES,
        source_ref=source,
        passage_refs=(passage,),
        source_snapshot_sha256="2" * 64,
        coverage_sha256="3" * 64,
        coverage_grade=SelectedArchiveCoverageGrade.COMPLETE,
        origin_boundary_user_message_id=origin_boundary_id,
    )
    selected = ArchiveSearchSelectedEvidence(
        corpus=ArchiveSearchCorpus.MESSAGES,
        source_ref=source,
        passage_refs=(passage,),
        resolved_snapshot_sha256=evidence.source_snapshot_sha256,
    )
    return evidence, selected


def _archive_metadata(
    *,
    answer: str,
    selected: ArchiveSearchSelectedEvidence,
    evidence: SelectedArchiveEvidence,
    structural: dict[str, object] | None = None,
) -> tuple[dict[str, Any], ArchiveRecallOutcome, str]:
    outcome = ArchiveRecallOutcome(
        lane=ArchiveRecallLane.FEDERATED_SEARCH,
        status=ArchiveRecallStatus.COMPLETE,
        plan_sha256="4" * 64,
        evidence_sha256="5" * 64,
        coverage_sha256=evidence.coverage_sha256,
        coverage_grade=ArchiveSearchCoverageGrade.COMPLETE,
        candidate_count=1,
        used_citation_labels=("A1.1",),
        selected_evidence=selected,
        publication_attested=True,
        semantic_verified=False,
        answer_sha256=_sha(answer),
    )
    metadata: dict[str, Any] = {"structural": structural or {"answer_present": True}}
    receipt = attach_accepted_archive_recall_outcome_receipt(metadata, outcome)
    return metadata, outcome, receipt.outcome_sha256


def _insert_work_and_selected(
    conn: sqlite3.Connection,
    *,
    owner: str,
    conversation_id: str,
    boundary_id: str,
    assistant_id: str,
    evidence: SelectedArchiveEvidence,
    plan_sha256: str,
    outcome_sha256: str,
) -> None:
    conn.execute(
        """INSERT INTO work_items(
               id,user_id,conversation_id,kind,goal,state,playbook,
               completion_contract,active_frame_json,anchor_user_message_id,
               anchor_assistant_message_id,accepted_plan_sha256,
               accepted_outcome_sha256,revision,transition,created_at,
               updated_at,expires_at,closed_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            evidence.work_item_id,
            owner,
            conversation_id,
            "compare_conversation_with_document",
            "compare_exact_message_evidence_with_document",
            "waiting_for_input",
            "compare_conversation_with_document",
            "accepted_exact_message_and_document_comparison",
            COMPARE_CONVERSATION_DOCUMENT_ACTIVE_FRAME_JSON,
            boundary_id,
            assistant_id,
            plan_sha256,
            outcome_sha256,
            1,
            "question_asked",
            _NOW,
            _NOW,
            _EXPIRES,
            None,
        ),
    )
    stored = evidence.to_storage_payload()
    conn.execute(
        """INSERT INTO work_item_selected_evidence(
               work_item_id,corpus,source_ref_json,passage_refs_json,
               source_snapshot_sha256,coverage_sha256,coverage_grade,
               origin_boundary_user_message_id
           ) VALUES(?,?,?,?,?,?,?,?)""",
        tuple(
            stored[key]
            for key in (
                "work_item_id",
                "corpus",
                "source_ref_json",
                "passage_refs_json",
                "source_snapshot_sha256",
                "coverage_sha256",
                "coverage_grade",
                "origin_boundary_user_message_id",
            )
        ),
    )


def _insert_question(
    conn: sqlite3.Connection,
    *,
    boundary_id: str,
    assistant_id: str,
    shape: DocumentReferenceAdmissionShape,
) -> None:
    conn.execute(
        """INSERT INTO work_item_compare_document_questions(
               id,work_item_id,kind,admission_shape,state,created_at,
               prompt_boundary_user_message_id,prompt_assistant_message_id,
               work_revision,candidate_set_id,answered_at,answer_user_message_id,
               selected_ordinal,accepted_search_plan_sha256,
               accepted_search_outcome_sha256,closed_at,close_reason
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _QUESTION_ID,
            _WORK_ID,
            "provide_document_reference",
            shape.value,
            "waiting",
            _NOW,
            boundary_id,
            assistant_id,
            1,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
    )


def _create_direct_waiting(storage: Any, owner: str = "compare-owner") -> tuple[Any, Any]:
    storage.ensure_user(owner, source="local")
    conversation = storage.create_conversation(owner, "private comparison title")
    boundary = storage.store_message(conversation["id"], owner, "user", "compare history with a file")
    evidence, selected = _selected_messages(
        storage,
        owner=owner,
        work_item_id=_WORK_ID,
        origin_boundary_id=boundary["id"],
    )
    answer = "Attach or name the document to compare."
    metadata, outcome, outcome_sha256 = _archive_metadata(
        answer=answer,
        selected=selected,
        evidence=evidence,
        structural={
            "answer_present": True,
            "model_spoke": False,
            "verdict_kind": COMPARE_DOCUMENT_REFERENCE_REQUIRED_VERDICT_KIND,
        },
    )
    assistant = storage.store_message(
        conversation["id"],
        owner,
        "assistant",
        answer,
        metadata=metadata,
        reply_to=boundary["id"],
    )
    with storage.transaction() as conn:
        _insert_work_and_selected(
            conn,
            owner=owner,
            conversation_id=conversation["id"],
            boundary_id=boundary["id"],
            assistant_id=assistant["id"],
            evidence=evidence,
            plan_sha256=outcome.plan_sha256,
            outcome_sha256=outcome_sha256,
        )
        _insert_question(
            conn,
            boundary_id=boundary["id"],
            assistant_id=assistant["id"],
            shape=DocumentReferenceAdmissionShape.DIRECT_COMPOUND,
        )
    return conversation, evidence


def _create_followup_waiting(storage: Any, owner: str = "compare-owner") -> tuple[Any, Any]:
    storage.ensure_user(owner, source="local")
    conversation = storage.create_conversation(owner, "followup comparison")
    origin_boundary = storage.store_message(conversation["id"], owner, "user", "find the exact conversation")
    evidence, selected = _selected_messages(
        storage,
        owner=owner,
        work_item_id=_WORK_ID,
        origin_boundary_id=origin_boundary["id"],
    )
    archive_answer = "Exact message evidence [A1.1]."
    archive_metadata, outcome, outcome_sha256 = _archive_metadata(
        answer=archive_answer,
        selected=selected,
        evidence=evidence,
    )
    origin_assistant = storage.store_message(
        conversation["id"],
        owner,
        "assistant",
        archive_answer,
        metadata=archive_metadata,
        reply_to=origin_boundary["id"],
    )
    prompt_boundary = storage.store_message(
        conversation["id"],
        owner,
        "user",
        "compare that with a document",
        reply_to=origin_assistant["id"],
    )
    prompt = storage.store_message(
        conversation["id"],
        owner,
        "assistant",
        COMPARE_DOCUMENT_REFERENCE_PROMPT,
        metadata={
            "structural": {
                "answer_present": True,
                "model_spoke": False,
                "verdict_kind": COMPARE_DOCUMENT_REFERENCE_REQUIRED_VERDICT_KIND,
            }
        },
        reply_to=prompt_boundary["id"],
    )
    with storage.transaction() as conn:
        _insert_work_and_selected(
            conn,
            owner=owner,
            conversation_id=conversation["id"],
            boundary_id=origin_boundary["id"],
            assistant_id=origin_assistant["id"],
            evidence=evidence,
            plan_sha256=outcome.plan_sha256,
            outcome_sha256=outcome_sha256,
        )
        _insert_question(
            conn,
            boundary_id=prompt_boundary["id"],
            assistant_id=prompt["id"],
            shape=DocumentReferenceAdmissionShape.SELECTED_EVIDENCE_FOLLOWUP,
        )
    return conversation, evidence


def _resolve_shared_tenant_attachment(
    storage: Any,
    *,
    conversation: Any,
    evidence: SelectedArchiveEvidence,
    owner: str = "compare-owner",
    tenant: str = "shared-tenant",
    extra_attachment_id: str | None = None,
) -> tuple[ResolvedDocumentIdentity, dict[str, Any]]:
    storage.ensure_user(tenant, source="local")
    body = "PRIVATE-DOCUMENT-BODY-CANARY"
    content_sha256 = _sha(body)
    raw = RawObject(
        id=new_id("raw"),
        user_id=tenant,
        source="upload",
        source_ref="telegram-file:compare-document",
        raw_content=body,
        content_type="file",
        content_hash=content_sha256,
        metadata_json={
            "filename": "PRIVATE-DOCUMENT-NAME.pdf",
            "sha256": content_sha256,
            "size_bytes": len(body.encode("utf-8")),
            "uploaded_by": owner,
        },
    )
    storage.store_raw_object(raw)
    answer = storage.store_message(
        conversation["id"],
        owner,
        "user",
        "the attached file",
        metadata={
            "conversation_uploaded_raw_ids": [
                raw.id,
                *(() if extra_attachment_id is None else (extra_attachment_id,)),
            ],
            "had_attachments": True,
        },
    )
    source = SourceRef(
        SourceKind.DOCUMENT,
        AuthorityScope.TENANT_PRINCIPAL,
        tenant,
        owner,
        CanonicalObjectKind.RAW_OBJECT,
        raw.id,
    )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO file_source_aliases(
                   user_id,uploaded_by,source_ref,raw_object_id,supplied_filename,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (tenant, owner, raw.source_ref, raw.id, "PRIVATE-DOCUMENT-NAME.pdf", _ANSWERED_AT),
        )
        conn.execute(
            """UPDATE work_item_compare_document_questions
                  SET state='answered',answered_at=?,answer_user_message_id=?,
                      closed_at=?,close_reason='answered'
                WHERE id=? AND state='waiting'""",
            (_ANSWERED_AT, answer["id"], _ANSWERED_AT, _QUESTION_ID),
        )
        cursor = conn.execute(
            """SELECT id,source,source_ref,content_type,received_at,content_hash,
                      raw_content AS _raw_content,metadata_json AS _raw_metadata
                 FROM raw_objects WHERE id=?""",
            (raw.id,),
        )
        raw_identity = raw_source_identity_sha256(dict(cursor.fetchone()))
        document = ResolvedDocumentIdentity(
            work_item_id=evidence.work_item_id,
            provenance=ResolvedDocumentProvenance.CURRENT_TURN_ATTACHMENT,
            source_ref=source,
            raw_object_id=raw.id,
            raw_source_identity_sha256=raw_identity,
            raw_content_sha256=content_sha256,
            content_sha256=content_sha256,
            candidate_source_snapshot_sha256=None,
            origin_boundary_user_message_id=answer["id"],
            resolved_revision=2,
            resolved_at=_RESOLVED_AT,
        )
        stored = document.to_storage_payload()
        conn.execute(
            """INSERT INTO work_item_compare_document_evidence(
                   work_item_id,provenance,source_ref_json,raw_object_id,
                   raw_source_identity_sha256,raw_content_sha256,content_sha256,
                   candidate_source_snapshot_sha256,origin_boundary_user_message_id,
                   resolved_revision,resolved_at,candidate_set_id,selected_ordinal
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(
                stored[key]
                for key in (
                    "work_item_id",
                    "provenance",
                    "source_ref_json",
                    "raw_object_id",
                    "raw_source_identity_sha256",
                    "raw_content_sha256",
                    "content_sha256",
                    "candidate_source_snapshot_sha256",
                    "origin_boundary_user_message_id",
                    "resolved_revision",
                    "resolved_at",
                    "candidate_set_id",
                    "selected_ordinal",
                )
            ),
        )
        updated = conn.execute(
            """UPDATE work_items
                  SET state='active',revision=2,transition='document_resolved',updated_at=?
                WHERE id=? AND revision=1 AND state='waiting_for_input'""",
            (_RESOLVED_AT, evidence.work_item_id),
        )
        assert updated.rowcount == 1
    return document, answer


def _candidate_projection(
    *,
    owner: str,
    tenant: str,
    selected_body: str = "PRIVATE-SELECTED-CANDIDATE-BODY",
) -> ArchiveSearchAcceptedCandidateProjection:
    candidates: list[ArchiveSearchCandidateProjectionEntry] = []
    for ordinal in (1, 2):
        raw_id = f"raw_{ordinal:016x}"
        source = SourceRef(
            SourceKind.DOCUMENT,
            AuthorityScope.TENANT_PRINCIPAL,
            tenant,
            owner,
            CanonicalObjectKind.RAW_OBJECT,
            raw_id,
        )
        passage = PassageRef(
            source,
            SourceRevision(
                SourceRepresentation(RepresentationKind.RAW_OBJECT, raw_id),
                RevisionKind.RAW_CONTENT_SHA256,
                _sha(selected_body if ordinal == 1 else "PRIVATE-SECOND-CANDIDATE-BODY"),
            ),
            TextSpanLocator(chunk_index=0, start_char=ordinal * 10, end_char=ordinal * 10 + 5),
            PASSAGE_INDEX_VERSION,
            EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
        )
        candidates.append(
            ArchiveSearchCandidateProjectionEntry(
                ordinal=ordinal,
                public_citation_label=f"A{ordinal}",
                corpus=ArchiveSearchCorpus.DOCUMENTS,
                source_ref=source,
                passage_refs=(passage,),
                resolved_snapshot_sha256=f"{ordinal + 5}" * 64,
            )
        )
    return _new_accepted_candidate_projection(
        candidates=tuple(candidates),
        coverage_grade=ArchiveSearchCoverageGrade.COMPLETE,
        coverage_sha256="a" * 64,
        evidence_sha256="b" * 64,
    )


def _advance_to_direct_candidate_question(
    storage: Any,
    *,
    conversation: Any,
    owner: str = "compare-owner",
    tenant: str = "shared-tenant",
    question_created_at: str = _ANSWERED_AT,
) -> ArchiveCandidateSet:
    storage.ensure_user(tenant, source="local")
    boundary = storage.store_message(
        conversation["id"],
        owner,
        "user",
        "use the quarterly report",
    )
    with storage.transaction() as conn:
        conn.execute(
            """UPDATE work_item_compare_document_questions
                  SET state='answered',answered_at=?,answer_user_message_id=?,
                      closed_at=?,close_reason='answered'
                WHERE id=? AND state='waiting'""",
            (_ANSWERED_AT, boundary["id"], _ANSWERED_AT, _QUESTION_ID),
        )
    projection = _candidate_projection(owner=owner, tenant=tenant)
    candidate_set = ArchiveCandidateSet.from_accepted_projection(
        id=_CANDIDATE_SET_ID,
        work_item_id=_WORK_ID,
        origin_boundary_user_message_id=boundary["id"],
        projection=projection,
    )
    answer = "Two exact documents match.\n\n" + archive_candidate_selection_offer_suffix(("A1", "A2"))
    outcome = ArchiveRecallOutcome(
        lane=ArchiveRecallLane.FEDERATED_SEARCH,
        status=ArchiveRecallStatus.COMPLETE,
        plan_sha256="9" * 64,
        evidence_sha256=projection.evidence_sha256,
        coverage_sha256=projection.coverage_sha256,
        coverage_grade=projection.coverage_grade,
        candidate_count=projection.candidate_count,
        used_citation_labels=("A1.1", "A2.1"),
        selected_evidence=None,
        publication_attested=True,
        semantic_verified=False,
        answer_sha256=_sha(answer),
        candidate_projection_sha256=projection.canonical_sha256,
    )
    metadata: dict[str, Any] = {"structural": {"answer_present": True}}
    receipt = attach_accepted_archive_recall_outcome_receipt(metadata, outcome)
    assistant = storage.store_message(
        conversation["id"],
        owner,
        "assistant",
        answer,
        metadata=metadata,
        reply_to=boundary["id"],
    )
    with storage.transaction() as conn:
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
        conn.execute(
            """INSERT INTO work_item_compare_document_questions(
                   id,work_item_id,kind,admission_shape,state,created_at,
                   prompt_boundary_user_message_id,prompt_assistant_message_id,
                   work_revision,candidate_set_id,answered_at,answer_user_message_id,
                   selected_ordinal,accepted_search_plan_sha256,
                   accepted_search_outcome_sha256,closed_at,close_reason
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _SECOND_QUESTION_ID,
                _WORK_ID,
                "select_document_candidate",
                "direct_compound",
                "waiting",
                question_created_at,
                boundary["id"],
                assistant["id"],
                2,
                _CANDIDATE_SET_ID,
                None,
                None,
                None,
                outcome.plan_sha256,
                receipt.outcome_sha256,
                None,
                None,
            ),
        )
        updated = conn.execute(
            """UPDATE work_items
                  SET revision=2,transition='question_reasked',updated_at=?,expires_at=?
                WHERE id=? AND revision=1 AND state='waiting_for_input'""",
            (_ANSWERED_AT, "2026-08-25T20:05:00+00:00", _WORK_ID),
        )
        assert updated.rowcount == 1
    return candidate_set


def _resolve_selected_candidate(
    storage: Any,
    *,
    conversation: Any,
    evidence: SelectedArchiveEvidence,
    candidate_set: ArchiveCandidateSet,
    owner: str = "compare-owner",
    tenant: str = "shared-tenant",
) -> tuple[ResolvedDocumentIdentity, dict[str, Any]]:
    selected = candidate_set.selected_evidence(1)
    raw_id = selected.source_ref.canonical_object_id
    body = "PRIVATE-SELECTED-CANDIDATE-BODY"
    content_sha256 = _sha(body)
    raw = RawObject(
        id=raw_id,
        user_id=tenant,
        source="upload",
        source_ref="telegram-file:selected-candidate",
        raw_content=body,
        content_type="file",
        content_hash=content_sha256,
        metadata_json={
            "filename": "PRIVATE-CANDIDATE-NAME.docx",
            "sha256": content_sha256,
            "size_bytes": len(body.encode("utf-8")),
            "uploaded_by": owner,
        },
    )
    storage.store_raw_object(raw)
    prompt_id = storage.execute(
        "SELECT prompt_assistant_message_id FROM work_item_compare_document_questions WHERE id=?",
        (_SECOND_QUESTION_ID,),
    ).fetchone()[0]
    answer = storage.store_message(
        conversation["id"],
        owner,
        "user",
        "1",
        reply_to=prompt_id,
    )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO file_source_aliases(
                   user_id,uploaded_by,source_ref,raw_object_id,supplied_filename,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (tenant, owner, raw.source_ref, raw.id, "PRIVATE-CANDIDATE-NAME.docx", _CANDIDATE_ANSWERED_AT),
        )
        conn.execute(
            """UPDATE work_item_compare_document_questions
                  SET state='answered',answered_at=?,answer_user_message_id=?,selected_ordinal=1,
                      closed_at=?,close_reason='answered'
                WHERE id=? AND state='waiting'""",
            (
                _CANDIDATE_ANSWERED_AT,
                answer["id"],
                _CANDIDATE_ANSWERED_AT,
                _SECOND_QUESTION_ID,
            ),
        )
        cursor = conn.execute(
            """SELECT id,source,source_ref,content_type,received_at,content_hash,
                      raw_content AS _raw_content,metadata_json AS _raw_metadata
                 FROM raw_objects WHERE id=?""",
            (raw.id,),
        )
        document = ResolvedDocumentIdentity(
            work_item_id=evidence.work_item_id,
            provenance=ResolvedDocumentProvenance.HISTORICAL_CANDIDATE_ORDINAL,
            source_ref=selected.source_ref,
            raw_object_id=raw.id,
            raw_source_identity_sha256=raw_source_identity_sha256(dict(cursor.fetchone())),
            raw_content_sha256=content_sha256,
            content_sha256=content_sha256,
            candidate_source_snapshot_sha256=selected.source_snapshot_sha256,
            origin_boundary_user_message_id=answer["id"],
            resolved_revision=3,
            resolved_at=_CANDIDATE_RESOLVED_AT,
            candidate_set_id=candidate_set.id,
            selected_ordinal=1,
        )
        stored = document.to_storage_payload()
        conn.execute(
            """INSERT INTO work_item_compare_document_evidence(
                   work_item_id,provenance,source_ref_json,raw_object_id,
                   raw_source_identity_sha256,raw_content_sha256,content_sha256,
                   candidate_source_snapshot_sha256,origin_boundary_user_message_id,
                   resolved_revision,resolved_at,candidate_set_id,selected_ordinal
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(
                stored[key]
                for key in (
                    "work_item_id",
                    "provenance",
                    "source_ref_json",
                    "raw_object_id",
                    "raw_source_identity_sha256",
                    "raw_content_sha256",
                    "content_sha256",
                    "candidate_source_snapshot_sha256",
                    "origin_boundary_user_message_id",
                    "resolved_revision",
                    "resolved_at",
                    "candidate_set_id",
                    "selected_ordinal",
                )
            ),
        )
        updated = conn.execute(
            """UPDATE work_items
                  SET state='active',revision=3,transition='document_resolved',updated_at=?
                WHERE id=? AND revision=2 AND state='waiting_for_input'""",
            (_CANDIDATE_RESOLVED_AT, _WORK_ID),
        )
        assert updated.rowcount == 1
    return document, answer


def _complete_comparison(
    storage: Any,
    *,
    conversation: Any,
    evidence: SelectedArchiveEvidence,
    document: ResolvedDocumentIdentity,
    answer_boundary: dict[str, Any],
    owner: str = "compare-owner",
    completed_at: str = _COMPLETED_AT,
    answer: str = "The exact comparison result [A1.1] [D1.1].",
) -> AcceptedComparisonResultIdentity:
    outcome = CompareConversationDocumentOutcome(
        plan_sha256="7" * 64,
        answer_sha256=_sha(answer),
        status=CompareConversationDocumentStatus(evidence.coverage_grade.value),
        message_coverage_grade=evidence.coverage_grade,
        document_verification_complete=True,
        publication_attested=True,
        semantic_verified=True,
        message_evidence_sha256=selected_evidence_sha256(evidence),
        document_evidence_sha256=document.canonical_sha256,
        evidence_bundle_sha256=comparison_evidence_bundle_sha256(evidence, document),
        model_evidence_sha256="8" * 64,
    )
    metadata: dict[str, object] = {"structural": {"answer_present": True, "model_spoke": True}}
    receipt = attach_accepted_comparison_outcome_receipt(metadata, outcome)
    assistant = storage.store_message(
        conversation["id"],
        owner,
        "assistant",
        answer,
        metadata=metadata,
        reply_to=answer_boundary["id"],
    )
    result = AcceptedComparisonResultIdentity(
        work_item_id=evidence.work_item_id,
        answer_boundary_user_message_id=answer_boundary["id"],
        answer_assistant_message_id=assistant["id"],
        accepted_plan_sha256=outcome.plan_sha256,
        accepted_outcome_sha256=receipt.outcome_sha256,
        comparison_status=outcome.status,
        message_coverage_grade=outcome.message_coverage_grade,
        document_verification_complete=outcome.document_verification_complete,
        publication_attested=outcome.publication_attested,
        semantic_verified=outcome.semantic_verified,
        message_evidence_sha256=outcome.message_evidence_sha256,
        document_evidence_sha256=outcome.document_evidence_sha256,
        evidence_bundle_sha256=outcome.evidence_bundle_sha256,
        model_evidence_sha256=outcome.model_evidence_sha256,
        completed_revision=document.resolved_revision + 1,
        completed_at=completed_at,
    )
    stored = result.to_storage_payload()
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO work_item_compare_outcomes(
                   work_item_id,answer_boundary_user_message_id,answer_assistant_message_id,
                   accepted_plan_sha256,accepted_outcome_sha256,comparison_status,
                   message_coverage_grade,document_verification_complete,
                   publication_attested,semantic_verified,message_evidence_sha256,
                   document_evidence_sha256,evidence_bundle_sha256,model_evidence_sha256,
                   completed_revision,completed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(
                stored[key]
                for key in (
                    "work_item_id",
                    "answer_boundary_user_message_id",
                    "answer_assistant_message_id",
                    "accepted_plan_sha256",
                    "accepted_outcome_sha256",
                    "comparison_status",
                    "message_coverage_grade",
                    "document_verification_complete",
                    "publication_attested",
                    "semantic_verified",
                    "message_evidence_sha256",
                    "document_evidence_sha256",
                    "evidence_bundle_sha256",
                    "model_evidence_sha256",
                    "completed_revision",
                    "completed_at",
                )
            ),
        )
        updated = conn.execute(
            """UPDATE work_items
                  SET state='completed',revision=?,transition='comparison_published',
                      updated_at=?,closed_at=?
                WHERE id=? AND revision=? AND state='active'""",
            (
                document.resolved_revision + 1,
                completed_at,
                completed_at,
                evidence.work_item_id,
                document.resolved_revision,
            ),
        )
        assert updated.rowcount == 1
    return result


def test_schema42_direct_compound_waiting_reader_is_restart_safe_and_body_free(storage: Any) -> None:
    conversation, _evidence = _create_direct_waiting(storage)

    with storage.transaction() as conn:
        item = get_current_compare_conversation_with_document_work_item_in_transaction(
            conn,
            user_id="compare-owner",
            conversation_id=conversation["id"],
            now="2026-08-25T09:00:00+00:00",
        )
        validate_work_item_schema(conn)

    assert item is not None
    assert item.state is WorkState.WAITING_FOR_INPUT
    assert item.transition is WorkTransition.QUESTION_ASKED
    assert item.document_questions[0].admission_shape is DocumentReferenceAdmissionShape.DIRECT_COMPOUND
    encoded = json.dumps(item.to_payload(), sort_keys=True)
    assert all(
        canary not in encoded
        for canary in (
            "PRIVATE-MESSAGE-BODY-CANARY",
            "private comparison title",
            "compare history with a file",
            "Attach or name the document",
            "excerpt",
            "chain_of_thought",
        )
    )


def test_selected_evidence_followup_admission_has_exact_adjacent_boundary(storage: Any) -> None:
    conversation, _evidence = _create_followup_waiting(storage)

    with storage.transaction() as conn:
        item = get_current_compare_conversation_with_document_work_item_in_transaction(
            conn,
            user_id="compare-owner",
            conversation_id=conversation["id"],
            now="2026-08-25T09:00:00+00:00",
        )

    assert item is not None
    assert (
        item.document_questions[0].admission_shape
        is DocumentReferenceAdmissionShape.SELECTED_EVIDENCE_FOLLOWUP
    )
    assert item.anchor_assistant_message_id != item.document_questions[0].prompt_assistant_message_id


def test_comparison_contract_rejects_tampered_receipt_and_result_revision() -> None:
    outcome = CompareConversationDocumentOutcome(
        plan_sha256="1" * 64,
        answer_sha256="2" * 64,
        status=CompareConversationDocumentStatus.COMPLETE,
        message_coverage_grade=SelectedArchiveCoverageGrade.COMPLETE,
        document_verification_complete=True,
        publication_attested=True,
        semantic_verified=True,
        message_evidence_sha256="3" * 64,
        document_evidence_sha256="4" * 64,
        evidence_bundle_sha256="5" * 64,
        model_evidence_sha256="6" * 64,
    )
    metadata: dict[str, object] = {}
    receipt = attach_accepted_comparison_outcome_receipt(metadata, outcome)
    assert receipt.outcome == outcome
    tampered = json.loads(json.dumps(metadata))
    tampered["accepted_compare_conversation_document_outcome"]["outcome"]["answer_sha256"] = "f" * 64
    from friday.interaction_control_plane.compare_conversation_document import (
        load_accepted_comparison_outcome_receipt,
    )

    with pytest.raises(WorkItemContractError, match="digest"):
        load_accepted_comparison_outcome_receipt(tampered)

    with pytest.raises(WorkItemContractError, match="revision"):
        AcceptedComparisonResultIdentity(
            work_item_id=_WORK_ID,
            answer_boundary_user_message_id="msg_1111111111111111",
            answer_assistant_message_id="msg_2222222222222222",
            accepted_plan_sha256="1" * 64,
            accepted_outcome_sha256="2" * 64,
            comparison_status=CompareConversationDocumentStatus.COMPLETE,
            message_coverage_grade=SelectedArchiveCoverageGrade.COMPLETE,
            document_verification_complete=True,
            publication_attested=True,
            semantic_verified=True,
            message_evidence_sha256="3" * 64,
            document_evidence_sha256="4" * 64,
            evidence_bundle_sha256="5" * 64,
            model_evidence_sha256="6" * 64,
            completed_revision=2,
            completed_at=_NOW,
        )


def test_direct_compound_ambiguity_preserves_order_and_shared_tenant(storage: Any) -> None:
    conversation, _evidence = _create_direct_waiting(storage)
    candidate_set = _advance_to_direct_candidate_question(storage, conversation=conversation)

    with storage.transaction() as conn:
        item = get_current_compare_conversation_with_document_work_item_in_transaction(
            conn,
            user_id="compare-owner",
            conversation_id=conversation["id"],
            now="2026-08-25T09:00:00+00:00",
        )

    assert item is not None
    assert item.revision == 2
    assert item.transition is WorkTransition.QUESTION_REASKED
    assert [candidate.ordinal for candidate in item.document_candidate_set.candidates] == [1, 2]
    assert item.document_candidate_set == candidate_set
    assert {candidate.source_ref.tenant_id for candidate in item.document_candidate_set.candidates} == {
        "shared-tenant"
    }
    assert all(
        question.admission_shape is DocumentReferenceAdmissionShape.DIRECT_COMPOUND
        for question in item.document_questions
    )

    impossible_second = replace(item.document_questions[1], created_at=_NOW)
    with pytest.raises(WorkItemContractError, match="timeline"):
        replace(
            item,
            document_questions=(item.document_questions[0], impossible_second),
        )
    future_second = replace(item.document_questions[1], created_at=_RESOLVED_AT)
    with pytest.raises(WorkItemContractError, match="timeline"):
        replace(
            item,
            document_questions=(item.document_questions[0], future_second),
        )


def test_candidate_question_cannot_predate_reference_answer(storage: Any) -> None:
    conversation, _evidence = _create_direct_waiting(storage)
    with pytest.raises(sqlite3.IntegrityError, match="question scope"):
        _advance_to_direct_candidate_question(
            storage,
            conversation=conversation,
            question_created_at=_NOW,
        )


def test_pending_boundary_reader_accepts_only_latest_valid_candidate_ordinal(storage: Any) -> None:
    conversation, _evidence = _create_direct_waiting(storage)
    _advance_to_direct_candidate_question(storage, conversation=conversation)
    prompt_id = storage.execute(
        "SELECT prompt_assistant_message_id FROM work_item_compare_document_questions WHERE id=?",
        (_SECOND_QUESTION_ID,),
    ).fetchone()[0]
    boundary = storage.store_message(
        conversation["id"],
        "compare-owner",
        "user",
        "1",
        reply_to=prompt_id,
    )

    with storage.transaction() as conn:
        with pytest.raises(WorkItemAnchorError, match="latest"):
            get_current_compare_conversation_with_document_work_item_in_transaction(
                conn,
                user_id="compare-owner",
                conversation_id=conversation["id"],
                now="2026-08-25T09:00:00+00:00",
            )
        item = get_current_compare_conversation_with_document_work_item_in_transaction(
            conn,
            user_id="compare-owner",
            conversation_id=conversation["id"],
            boundary_user_message_id=boundary["id"],
            now="2026-08-25T09:00:00+00:00",
        )
    assert item is not None

    storage.store_message(conversation["id"], "compare-owner", "user", "unrelated later turn")
    with storage.transaction() as conn, pytest.raises(WorkItemAnchorError, match="latest"):
        get_current_compare_conversation_with_document_work_item_in_transaction(
            conn,
            user_id="compare-owner",
            conversation_id=conversation["id"],
            boundary_user_message_id=boundary["id"],
            now="2026-08-25T09:00:00+00:00",
        )


def test_q2_answer_cannot_record_an_ordinal_other_than_user_content(storage: Any) -> None:
    conversation, _evidence = _create_direct_waiting(storage)
    _advance_to_direct_candidate_question(storage, conversation=conversation)
    prompt_id = storage.execute(
        "SELECT prompt_assistant_message_id FROM work_item_compare_document_questions WHERE id=?",
        (_SECOND_QUESTION_ID,),
    ).fetchone()[0]
    boundary = storage.store_message(
        conversation["id"],
        "compare-owner",
        "user",
        "2",
        reply_to=prompt_id,
    )

    with storage.transaction() as conn, pytest.raises(sqlite3.IntegrityError, match="question update"):
        conn.execute(
            """UPDATE work_item_compare_document_questions
                  SET state='answered',answered_at=?,answer_user_message_id=?,selected_ordinal=1,
                      closed_at=?,close_reason='answered'
                WHERE id=?""",
            (_CANDIDATE_ANSWERED_AT, boundary["id"], _CANDIDATE_ANSWERED_AT, _SECOND_QUESTION_ID),
        )


def test_candidate_ordinal_resolves_and_publishes_on_unchanged_schema42(storage: Any) -> None:
    conversation, evidence = _create_direct_waiting(storage)
    candidate_set = _advance_to_direct_candidate_question(storage, conversation=conversation)
    document, answer_boundary = _resolve_selected_candidate(
        storage,
        conversation=conversation,
        evidence=evidence,
        candidate_set=candidate_set,
    )

    with storage.transaction() as conn:
        active = get_current_compare_conversation_with_document_work_item_in_transaction(
            conn,
            user_id="compare-owner",
            conversation_id=conversation["id"],
            now=_CANDIDATE_RESOLVED_AT,
        )
    assert active is not None
    assert active.revision == 3
    assert active.resolved_document_evidence == document
    assert document.candidate_source_snapshot_sha256 == candidate_set.candidates[0].source_snapshot_sha256
    assert document.candidate_source_snapshot_sha256 != document.raw_content_sha256
    wrong_projection = _candidate_projection(
        owner="compare-owner",
        tenant="shared-tenant",
        selected_body="STALE-SELECTED-CANDIDATE-BODY",
    )
    wrong_candidate_set = ArchiveCandidateSet.from_accepted_projection(
        id=candidate_set.id,
        work_item_id=candidate_set.work_item_id,
        origin_boundary_user_message_id=candidate_set.origin_boundary_user_message_id,
        projection=wrong_projection,
    )
    with pytest.raises(WorkItemContractError, match="Raw revision"):
        replace(active, document_candidate_set=wrong_candidate_set)

    result = _complete_comparison(
        storage,
        conversation=conversation,
        evidence=evidence,
        document=document,
        answer_boundary=answer_boundary,
        completed_at=_CANDIDATE_COMPLETED_AT,
    )
    with storage.transaction() as conn:
        completed = get_compare_conversation_with_document_work_item_in_transaction(
            conn,
            work_item_id=_WORK_ID,
            user_id="compare-owner",
            conversation_id=conversation["id"],
        )
        validate_work_item_schema(conn)
    assert completed is not None
    assert completed.revision == 4
    assert completed.accepted_comparison == result


def test_shared_tenant_attachment_active_and_completed_reader_survive_restart(storage: Any) -> None:
    conversation, evidence = _create_direct_waiting(storage)
    document, answer_boundary = _resolve_shared_tenant_attachment(
        storage,
        conversation=conversation,
        evidence=evidence,
    )

    with storage.transaction() as conn:
        active = get_current_compare_conversation_with_document_work_item_in_transaction(
            conn,
            user_id="compare-owner",
            conversation_id=conversation["id"],
            now=_RESOLVED_AT,
        )
    assert active is not None
    assert active.state is WorkState.ACTIVE
    assert active.resolved_document_evidence == document
    assert document.source_ref.tenant_id == "shared-tenant"
    assert document.source_ref.principal_id == "compare-owner"

    result = _complete_comparison(
        storage,
        conversation=conversation,
        evidence=evidence,
        document=document,
        answer_boundary=answer_boundary,
    )
    with storage.transaction() as conn:
        completed = get_compare_conversation_with_document_work_item_in_transaction(
            conn,
            work_item_id=_WORK_ID,
            user_id="compare-owner",
            conversation_id=conversation["id"],
        )
        stale = conn.execute(
            """UPDATE work_items SET revision=4,updated_at=?
                 WHERE id=? AND revision=2""",
            (_COMPLETED_AT, _WORK_ID),
        )
    assert stale.rowcount == 0
    assert completed is not None
    assert completed.state is WorkState.COMPLETED
    assert completed.transition is WorkTransition.COMPARISON_PUBLISHED
    assert completed.accepted_comparison == result
    encoded = json.dumps(completed.to_payload(), sort_keys=True)
    assert "PRIVATE-DOCUMENT-BODY-CANARY" not in encoded
    assert "PRIVATE-DOCUMENT-NAME.pdf" not in encoded


def test_active_reader_requires_latest_attachment_boundary_and_exact_membership(storage: Any) -> None:
    conversation, evidence = _create_direct_waiting(storage)
    document, answer_boundary = _resolve_shared_tenant_attachment(
        storage,
        conversation=conversation,
        evidence=evidence,
    )
    storage.store_message(conversation["id"], "compare-owner", "user", "unrelated later turn")
    with storage.transaction() as conn, pytest.raises(WorkItemAnchorError, match="latest"):
        get_current_compare_conversation_with_document_work_item_in_transaction(
            conn,
            user_id="compare-owner",
            conversation_id=conversation["id"],
            now=_RESOLVED_AT,
        )

    storage.execute(
        "UPDATE messages SET metadata_json=? WHERE id=?",
        (
            json.dumps(
                {
                    "conversation_uploaded_raw_ids": [
                        document.raw_object_id,
                        "raw_deadbeefdeadbeef",
                    ]
                }
            ),
            answer_boundary["id"],
        ),
    )
    with storage.transaction() as conn, pytest.raises(WorkItemAnchorError, match="attachment set"):
        get_compare_conversation_with_document_work_item_in_transaction(
            conn,
            work_item_id=_WORK_ID,
            user_id="compare-owner",
            conversation_id=conversation["id"],
        )


def test_document_resolution_rejects_ambiguous_attachment_array(storage: Any) -> None:
    conversation, evidence = _create_direct_waiting(storage)
    with pytest.raises(sqlite3.IntegrityError, match="document evidence scope"):
        _resolve_shared_tenant_attachment(
            storage,
            conversation=conversation,
            evidence=evidence,
            extra_attachment_id="raw_deadbeefdeadbeef",
        )


def test_comparison_accepts_detailed_multibyte_answer(storage: Any) -> None:
    conversation, evidence = _create_direct_waiting(storage)
    document, answer_boundary = _resolve_shared_tenant_attachment(
        storage,
        conversation=conversation,
        evidence=evidence,
    )
    answer = "Я" * 3_000
    result = _complete_comparison(
        storage,
        conversation=conversation,
        evidence=evidence,
        document=document,
        answer_boundary=answer_boundary,
        answer=answer,
    )
    with storage.transaction() as conn:
        completed = get_compare_conversation_with_document_work_item_in_transaction(
            conn,
            work_item_id=_WORK_ID,
            user_id="compare-owner",
            conversation_id=conversation["id"],
        )
    assert completed is not None
    assert completed.accepted_comparison == result


def test_schema_validator_rejects_orphan_comparison_sidecar(storage: Any) -> None:
    _conversation, _evidence = _create_direct_waiting(storage)
    copy = sqlite3.connect(":memory:")
    try:
        storage.conn.backup(copy)
        copy.execute("PRAGMA foreign_keys=OFF")
        trigger_row = copy.execute(
            """SELECT sql FROM sqlite_master
                WHERE type='trigger' AND name='trg_work_item_compare_questions_insert'"""
        ).fetchone()
        assert trigger_row is not None and isinstance(trigger_row[0], str)
        copy.execute("DROP TRIGGER trg_work_item_compare_questions_insert")
        source_question = copy.execute(
            "SELECT * FROM work_item_compare_document_questions WHERE work_item_id=?",
            (_WORK_ID,),
        ).fetchone()
        assert source_question is not None
        columns = tuple(
            str(column[1])
            for column in copy.execute("PRAGMA table_info(work_item_compare_document_questions)")
        )
        orphan = list(source_question)
        orphan[columns.index("id")] = "question_deadbeefdeadbeef"
        orphan[columns.index("work_item_id")] = "work_deadbeefdeadbeef"
        copy.execute(
            f"INSERT INTO work_item_compare_document_questions VALUES({','.join('?' for _ in orphan)})",
            orphan,
        )
        copy.execute(trigger_row[0])
        with pytest.raises(sqlite3.DatabaseError, match="sidecar ownership"):
            validate_work_item_schema(copy)
    finally:
        copy.close()


def test_completed_reader_fails_closed_after_sidecar_tamper(storage: Any) -> None:
    conversation, evidence = _create_direct_waiting(storage)
    document, answer_boundary = _resolve_shared_tenant_attachment(
        storage,
        conversation=conversation,
        evidence=evidence,
    )
    _complete_comparison(
        storage,
        conversation=conversation,
        evidence=evidence,
        document=document,
        answer_boundary=answer_boundary,
    )

    with storage.transaction() as conn:
        conn.execute("DROP TRIGGER trg_work_item_compare_outcomes_update")
        conn.execute(
            "UPDATE work_item_compare_outcomes SET evidence_bundle_sha256=? WHERE work_item_id=?",
            ("f" * 64, _WORK_ID),
        )
        with pytest.raises(WorkItemContractError, match="invalid"):
            get_compare_conversation_with_document_work_item_in_transaction(
                conn,
                work_item_id=_WORK_ID,
                user_id="compare-owner",
                conversation_id=conversation["id"],
            )


def test_export_conversation_retirement_and_account_inventory_cover_compare_sidecars(
    storage: Any,
) -> None:
    owner = "local:compare-lifecycle-owner"
    conversation, _evidence = _create_direct_waiting(storage, owner)

    exported = storage.export_user(owner)
    payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
    assert [work["id"] for work in payload["work_items"]] == [_WORK_ID]
    assert payload["work_items"][0]["document_questions"][0]["state"] == "waiting"
    assert "PRIVATE-MESSAGE-BODY-CANARY" not in json.dumps(payload["work_items"])

    report = storage.delete_conversation(conversation["id"], owner)
    assert report["cancelled"] == {"work_items": 1}
    row = storage.execute(
        """SELECT work.state,question.state,question.close_reason
             FROM work_items work
             JOIN work_item_compare_document_questions question
               ON question.work_item_id=work.id
            WHERE work.id=?""",
        (_WORK_ID,),
    ).fetchone()
    assert tuple(row) == ("cancelled", "closed", "cancelled")

    assert _mark_account_deletion_history_clean(storage, owner)
    storage.update_user(owner, status="disabled")
    plan = preflight_account_deletion(storage, owner, quiescence_available=True)
    assert plan["counts"]["work_items"] == 1
    assert plan["counts"]["work_item_compare_document_questions"] == 1
    assert plan["unknown_scopes"] == []


def _create_writer_followup_waiting(
    storage: Any,
    *,
    owner: str = "compare-writer-owner",
) -> tuple[dict[str, Any], Any, SelectedArchiveEvidence]:
    storage.ensure_user(owner, source="local")
    conversation = storage.create_conversation(owner, "writer followup")
    origin_boundary = storage.store_message(
        conversation["id"],
        owner,
        "user",
        "find exact message evidence",
    )
    default_owner = owner == "compare-writer-owner"
    old_work_id = (
        "work_3131313131313131"
        if default_owner
        else new_compare_conversation_with_document_work_item_id()
    )
    compare_work_id = (
        _WORK_ID if default_owner else new_compare_conversation_with_document_work_item_id()
    )
    compare_question_id = _QUESTION_ID if default_owner else new_compare_document_question_id()
    old_evidence, selected = _selected_messages(
        storage,
        owner=owner,
        work_item_id=old_work_id,
        origin_boundary_id=origin_boundary["id"],
    )
    archive_answer = "Exact message evidence [A1.1]."
    archive_metadata, outcome, outcome_sha256 = _archive_metadata(
        answer=archive_answer,
        selected=selected,
        evidence=old_evidence,
    )
    origin_assistant = storage.store_message(
        conversation["id"],
        owner,
        "assistant",
        archive_answer,
        metadata=archive_metadata,
        reply_to=origin_boundary["id"],
    )
    with storage.transaction() as conn:
        create_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            user_id=owner,
            conversation_id=conversation["id"],
            selected_evidence=old_evidence,
            anchor_user_message_id=origin_boundary["id"],
            anchor_assistant_message_id=origin_assistant["id"],
            accepted_plan_sha256=outcome.plan_sha256,
            accepted_outcome_sha256=outcome_sha256,
            now=_NOW,
        )
    followup = storage.store_message(
        conversation["id"],
        owner,
        "user",
        "compare those messages with a document",
        reply_to=origin_assistant["id"],
    )
    prompt = storage.store_message(
        conversation["id"],
        owner,
        "assistant",
        COMPARE_DOCUMENT_REFERENCE_PROMPT,
        metadata={
            "structural": {
                "answer_present": True,
                "model_spoke": False,
                "verdict_kind": COMPARE_DOCUMENT_REFERENCE_REQUIRED_VERDICT_KIND,
            }
        },
        reply_to=followup["id"],
    )
    with storage.transaction() as conn:
        item = create_compare_conversation_with_document_from_selected_followup_in_transaction(
            conn,
            selected_work_item_id=old_work_id,
            user_id=owner,
            conversation_id=conversation["id"],
            expected_selected_revision=1,
            prompt_boundary_user_message_id=followup["id"],
            prompt_assistant_message_id=prompt["id"],
            work_item_id=compare_work_id,
            question_id=compare_question_id,
            now=_NOW,
        )
    return conversation, item, old_evidence


def _prepare_writer_document(
    storage: Any,
    *,
    conversation: dict[str, Any],
    item: Any,
    owner: str = "compare-writer-owner",
    tenant: str = "compare-shared-tenant",
    provenance: ResolvedDocumentProvenance = ResolvedDocumentProvenance.CURRENT_TURN_ATTACHMENT,
    raw_identity_override: str | None = None,
) -> tuple[dict[str, Any], ResolvedDocumentIdentity]:
    storage.ensure_user(tenant, source="local")
    body = "WRITER-PRIVATE-DOCUMENT-BODY"
    digest = _sha(body)
    raw = RawObject(
        id=new_id("raw"),
        user_id=tenant,
        source="upload",
        source_ref="telegram-file:writer-document",
        raw_content=body,
        content_type="file",
        content_hash=digest,
        metadata_json={
            "filename": "writer-private-document.pdf",
            "sha256": digest,
            "size_bytes": len(body.encode("utf-8")),
            "uploaded_by": owner,
        },
    )
    storage.store_raw_object(raw)
    metadata_key = (
        "conversation_uploaded_raw_ids"
        if provenance is ResolvedDocumentProvenance.CURRENT_TURN_ATTACHMENT
        else "conversation_attachment_raw_ids"
    )
    boundary = storage.store_message(
        conversation["id"],
        owner,
        "user",
        "this exact document",
        metadata={metadata_key: [raw.id], "had_attachments": True},
        reply_to=item.document_questions[-1].prompt_assistant_message_id,
    )
    source = SourceRef(
        SourceKind.DOCUMENT,
        AuthorityScope.TENANT_PRINCIPAL,
        tenant,
        owner,
        CanonicalObjectKind.RAW_OBJECT,
        raw.id,
    )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO file_source_aliases(
                   user_id,uploaded_by,source_ref,raw_object_id,supplied_filename,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (tenant, owner, raw.source_ref, raw.id, "writer-private-document.pdf", _ANSWERED_AT),
        )
        cursor = conn.execute(
            """SELECT id,source,source_ref,content_type,received_at,content_hash,
                      raw_content AS _raw_content,metadata_json AS _raw_metadata
                 FROM raw_objects WHERE id=?""",
            (raw.id,),
        )
        raw_identity = raw_source_identity_sha256(dict(cursor.fetchone()))
    document = ResolvedDocumentIdentity(
        work_item_id=item.id,
        provenance=provenance,
        source_ref=source,
        raw_object_id=raw.id,
        raw_source_identity_sha256=raw_identity_override or raw_identity,
        raw_content_sha256=digest,
        content_sha256=digest,
        candidate_source_snapshot_sha256=None,
        origin_boundary_user_message_id=boundary["id"],
        resolved_revision=item.revision + 1,
        resolved_at=_RESOLVED_AT,
    )
    return boundary, document


def _writer_comparison_result(
    storage: Any,
    *,
    conversation: dict[str, Any],
    item: Any,
    owner: str = "compare-writer-owner",
    document_digest_override: str | None = None,
) -> AcceptedComparisonResultIdentity:
    document = item.resolved_document_evidence
    assert document is not None
    answer = "Accepted exact comparison [A1.1] [D1.1]."
    outcome = CompareConversationDocumentOutcome(
        plan_sha256="7" * 64,
        answer_sha256=_sha(answer),
        status=CompareConversationDocumentStatus(item.selected_message_evidence.coverage_grade.value),
        message_coverage_grade=item.selected_message_evidence.coverage_grade,
        document_verification_complete=True,
        publication_attested=True,
        semantic_verified=True,
        message_evidence_sha256=selected_evidence_sha256(item.selected_message_evidence),
        document_evidence_sha256=document_digest_override or document.canonical_sha256,
        evidence_bundle_sha256=comparison_evidence_bundle_sha256(
            item.selected_message_evidence,
            document,
        ),
        model_evidence_sha256="8" * 64,
    )
    metadata: dict[str, object] = {"structural": {"answer_present": True, "model_spoke": True}}
    receipt = attach_accepted_comparison_outcome_receipt(metadata, outcome)
    boundary_id = item.document_questions[-1].answer_user_message_id
    assert boundary_id is not None
    assistant = storage.store_message(
        conversation["id"],
        owner,
        "assistant",
        answer,
        metadata=metadata,
        reply_to=boundary_id,
    )
    return AcceptedComparisonResultIdentity(
        work_item_id=item.id,
        answer_boundary_user_message_id=boundary_id,
        answer_assistant_message_id=assistant["id"],
        accepted_plan_sha256=outcome.plan_sha256,
        accepted_outcome_sha256=receipt.outcome_sha256,
        comparison_status=outcome.status,
        message_coverage_grade=outcome.message_coverage_grade,
        document_verification_complete=True,
        publication_attested=True,
        semantic_verified=True,
        message_evidence_sha256=outcome.message_evidence_sha256,
        document_evidence_sha256=outcome.document_evidence_sha256,
        evidence_bundle_sha256=outcome.evidence_bundle_sha256,
        model_evidence_sha256=outcome.model_evidence_sha256,
        completed_revision=item.revision + 1,
        completed_at=_COMPLETED_AT,
    )


def test_writer_selected_followup_clones_exact_evidence_and_retires_old_atomically(
    storage: Any,
) -> None:
    conversation, item, old_evidence = _create_writer_followup_waiting(storage)

    assert item.state is WorkState.WAITING_FOR_INPUT
    assert item.revision == 1
    assert item.selected_message_evidence.work_item_id == _WORK_ID
    assert item.selected_message_evidence.passage_refs == old_evidence.passage_refs
    assert item.selected_message_evidence.source_ref == old_evidence.source_ref
    assert (
        item.document_questions[0].admission_shape
        is DocumentReferenceAdmissionShape.SELECTED_EVIDENCE_FOLLOWUP
    )
    old = storage.execute("SELECT state,revision FROM work_items WHERE id='work_3131313131313131'").fetchone()
    assert tuple(old) == ("suspended", 2)
    with storage.transaction() as conn:
        restarted = get_current_compare_conversation_with_document_work_item_in_transaction(
            conn,
            user_id="compare-writer-owner",
            conversation_id=conversation["id"],
            now="2026-08-25T09:00:00+00:00",
        )
    assert restarted == item
    assert new_compare_conversation_with_document_work_item_id().startswith("work_")
    assert new_compare_document_question_id().startswith("question_")


def test_writer_q1_resolution_is_shared_tenant_restart_safe_and_rolls_back_tamper(
    storage: Any,
) -> None:
    conversation, item, _old = _create_writer_followup_waiting(storage)
    boundary, tampered = _prepare_writer_document(
        storage,
        conversation=conversation,
        item=item,
        raw_identity_override="f" * 64,
    )
    with storage.transaction() as conn:
        with pytest.raises(WorkItemAnchorError, match="identity"):
            resolve_compare_conversation_document_reference_in_transaction(
                conn,
                work_item_id=item.id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                expected_revision=1,
                boundary_user_message_id=boundary["id"],
                document_evidence=tampered,
                now=_RESOLVED_AT,
            )
        question = conn.execute(
            "SELECT state FROM work_item_compare_document_questions WHERE work_item_id=?",
            (item.id,),
        ).fetchone()
        assert tuple(question) == ("waiting",)
        assert (
            conn.execute(
                "SELECT 1 FROM work_item_compare_document_evidence WHERE work_item_id=?",
                (item.id,),
            ).fetchone()
            is None
        )
        raw_cursor = conn.execute(
            """SELECT id,source,source_ref,content_type,received_at,content_hash,
                      raw_content AS _raw_content,metadata_json AS _raw_metadata
                 FROM raw_objects WHERE id=?""",
            (tampered.raw_object_id,),
        )
        exact = replace(
            tampered,
            raw_source_identity_sha256=raw_source_identity_sha256(dict(raw_cursor.fetchone())),
        )
        active = resolve_compare_conversation_document_reference_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=1,
            boundary_user_message_id=boundary["id"],
            document_evidence=exact,
            now=_RESOLVED_AT,
        )
    assert active.state is WorkState.ACTIVE
    assert active.revision == 2
    assert active.resolved_document_evidence is not None
    assert active.resolved_document_evidence.source_ref.tenant_id == "compare-shared-tenant"
    with storage.transaction() as conn, pytest.raises(WorkItemConflictError):
        resolve_compare_conversation_document_reference_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=1,
            boundary_user_message_id=boundary["id"],
            document_evidence=exact,
            now=_RESOLVED_AT,
        )


def test_writer_q1_ambiguity_and_q2_ordinal_activate_frozen_candidate(storage: Any) -> None:
    owner = "compare-writer-owner"
    tenant = "compare-candidate-tenant"
    conversation, item, _old = _create_writer_followup_waiting(storage, owner=owner)
    storage.ensure_user(tenant, source="local")
    boundary = storage.store_message(
        conversation["id"],
        owner,
        "user",
        "the quarterly report",
        reply_to=item.document_questions[0].prompt_assistant_message_id,
    )
    projection = _candidate_projection(owner=owner, tenant=tenant)
    answer = "Two exact documents match.\n\n" + archive_candidate_selection_offer_suffix(("A1", "A2"))
    outcome = ArchiveRecallOutcome(
        lane=ArchiveRecallLane.FEDERATED_SEARCH,
        status=ArchiveRecallStatus.COMPLETE,
        plan_sha256="9" * 64,
        evidence_sha256=projection.evidence_sha256,
        coverage_sha256=projection.coverage_sha256,
        coverage_grade=projection.coverage_grade,
        candidate_count=projection.candidate_count,
        used_citation_labels=("A1.1", "A2.1"),
        selected_evidence=None,
        publication_attested=True,
        semantic_verified=False,
        answer_sha256=_sha(answer),
        candidate_projection_sha256=projection.canonical_sha256,
    )
    metadata: dict[str, Any] = {"structural": {"answer_present": True}}
    receipt = attach_accepted_archive_recall_outcome_receipt(metadata, outcome)
    assistant = storage.store_message(
        conversation["id"],
        owner,
        "assistant",
        answer,
        metadata=metadata,
        reply_to=boundary["id"],
    )
    with storage.transaction() as conn:
        waiting_q2 = ask_compare_conversation_document_candidate_question_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=owner,
            conversation_id=conversation["id"],
            expected_revision=1,
            boundary_user_message_id=boundary["id"],
            candidate_question_assistant_message_id=assistant["id"],
            accepted_candidate_projection=projection,
            accepted_search_plan_sha256=outcome.plan_sha256,
            accepted_search_outcome_sha256=receipt.outcome_sha256,
            candidate_set_id=_CANDIDATE_SET_ID,
            question_id=_SECOND_QUESTION_ID,
            now=_ANSWERED_AT,
        )
    assert waiting_q2.revision == 2
    assert waiting_q2.document_candidate_set is not None
    assert waiting_q2.document_questions[-1].state is DocumentReferenceQuestionState.WAITING

    selected = waiting_q2.document_candidate_set.selected_evidence(1)
    body = "PRIVATE-SELECTED-CANDIDATE-BODY"
    digest = _sha(body)
    raw = RawObject(
        id=selected.source_ref.canonical_object_id,
        user_id=tenant,
        source="upload",
        source_ref="telegram-file:selected-writer-candidate",
        raw_content=body,
        content_type="file",
        content_hash=digest,
        metadata_json={
            "filename": "selected-writer-candidate.docx",
            "sha256": digest,
            "size_bytes": len(body.encode("utf-8")),
            "uploaded_by": owner,
        },
    )
    storage.store_raw_object(raw)
    ordinal_boundary = storage.store_message(
        conversation["id"],
        owner,
        "user",
        "1",
        reply_to=waiting_q2.document_questions[-1].prompt_assistant_message_id,
    )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO file_source_aliases(
                   user_id,uploaded_by,source_ref,raw_object_id,supplied_filename,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (tenant, owner, raw.source_ref, raw.id, "selected-writer-candidate.docx", _CANDIDATE_ANSWERED_AT),
        )
        raw_cursor = conn.execute(
            """SELECT id,source,source_ref,content_type,received_at,content_hash,
                      raw_content AS _raw_content,metadata_json AS _raw_metadata
                 FROM raw_objects WHERE id=?""",
            (raw.id,),
        )
        document = ResolvedDocumentIdentity(
            work_item_id=item.id,
            provenance=ResolvedDocumentProvenance.HISTORICAL_CANDIDATE_ORDINAL,
            source_ref=selected.source_ref,
            raw_object_id=raw.id,
            raw_source_identity_sha256=raw_source_identity_sha256(dict(raw_cursor.fetchone())),
            raw_content_sha256=digest,
            content_sha256=digest,
            candidate_source_snapshot_sha256=selected.source_snapshot_sha256,
            origin_boundary_user_message_id=ordinal_boundary["id"],
            resolved_revision=3,
            resolved_at=_CANDIDATE_RESOLVED_AT,
            candidate_set_id=_CANDIDATE_SET_ID,
            selected_ordinal=1,
        )
        active = resolve_compare_conversation_document_candidate_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=owner,
            conversation_id=conversation["id"],
            expected_revision=2,
            boundary_user_message_id=ordinal_boundary["id"],
            selected_ordinal=1,
            document_evidence=document,
            now=_CANDIDATE_RESOLVED_AT,
        )
    assert active.state is WorkState.ACTIVE
    assert active.revision == 3
    assert active.resolved_document_evidence == document
    with storage.transaction() as conn:
        restarted = get_compare_conversation_with_document_work_item_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=owner,
            conversation_id=conversation["id"],
        )
    assert restarted == active


def test_writer_completion_is_atomic_latest_and_digest_bound(storage: Any) -> None:
    conversation, waiting, _old = _create_writer_followup_waiting(storage)
    boundary, document = _prepare_writer_document(
        storage,
        conversation=conversation,
        item=waiting,
    )
    with storage.transaction() as conn:
        active = resolve_compare_conversation_document_reference_in_transaction(
            conn,
            work_item_id=waiting.id,
            user_id=waiting.user_id,
            conversation_id=waiting.conversation_id,
            expected_revision=1,
            boundary_user_message_id=boundary["id"],
            document_evidence=document,
            now=_RESOLVED_AT,
        )
    result = _writer_comparison_result(
        storage,
        conversation=conversation,
        item=active,
    )
    with storage.transaction() as conn:
        completed = complete_compare_conversation_with_document_in_transaction(
            conn,
            work_item_id=active.id,
            user_id=active.user_id,
            conversation_id=active.conversation_id,
            expected_revision=2,
            accepted_result=result,
            now=_COMPLETED_AT,
        )
    assert completed.state is WorkState.COMPLETED
    assert completed.accepted_comparison == result
    with storage.transaction() as conn, pytest.raises(WorkItemConflictError):
        complete_compare_conversation_with_document_in_transaction(
            conn,
            work_item_id=active.id,
            user_id=active.user_id,
            conversation_id=active.conversation_id,
            expected_revision=2,
            accepted_result=result,
            now=_COMPLETED_AT,
        )


def test_writer_lifecycle_closes_q1_and_expire_due_is_owner_scoped(storage: Any) -> None:
    conversation, waiting, _old = _create_writer_followup_waiting(storage)
    with storage.transaction() as conn:
        suspended = suspend_compare_conversation_with_document_in_transaction(
            conn,
            work_item_id=waiting.id,
            user_id=waiting.user_id,
            conversation_id=conversation["id"],
            expected_revision=1,
            now=_ANSWERED_AT,
        )
        cancelled = cancel_compare_conversation_with_document_in_transaction(
            conn,
            work_item_id=suspended.id,
            user_id=suspended.user_id,
            conversation_id=suspended.conversation_id,
            expected_revision=2,
            now=_RESOLVED_AT,
        )
    assert cancelled.state is WorkState.CANCELLED
    assert cancelled.document_questions[-1].close_reason.value == "suspended"

    other_conversation, other, _old = _create_writer_followup_waiting(
        storage,
        owner="compare-expire-owner",
    )
    with storage.transaction() as conn:
        count = expire_due_compare_conversation_with_document_work_items_in_transaction(
            conn,
            user_id="compare-expire-owner",
            now="2026-08-25T20:01:00+00:00",
        )
        expired = get_compare_conversation_with_document_work_item_in_transaction(
            conn,
            work_item_id=other.id,
            user_id=other.user_id,
            conversation_id=other_conversation["id"],
        )
    assert count == 1
    assert expired is not None and expired.state is WorkState.EXPIRED
    assert expired.document_questions[-1].close_reason.value == "expired"


def test_writer_expire_rejects_not_due_without_partial_question_close(storage: Any) -> None:
    conversation, waiting, _old = _create_writer_followup_waiting(storage)
    with storage.transaction() as conn:
        with pytest.raises(WorkItemConflictError):
            expire_compare_conversation_with_document_in_transaction(
                conn,
                work_item_id=waiting.id,
                user_id=waiting.user_id,
                conversation_id=conversation["id"],
                expected_revision=1,
                now=_ANSWERED_AT,
            )
        state = conn.execute(
            "SELECT state FROM work_item_compare_document_questions WHERE work_item_id=?",
            (waiting.id,),
        ).fetchone()
    assert tuple(state) == ("waiting",)
