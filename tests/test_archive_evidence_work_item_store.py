from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from friday.interaction_control_plane.archive_evidence_work_item_store import (
    accept_recall_selected_archive_evidence_replay_in_transaction,
    create_recall_selected_archive_evidence_work_item_in_transaction,
    expire_due_recall_selected_archive_evidence_work_items_in_transaction,
    expire_recall_selected_archive_evidence_work_item_in_transaction,
    get_current_recall_selected_archive_evidence_work_item_in_transaction,
    get_recall_selected_archive_evidence_work_item_for_export_in_transaction,
    get_recall_selected_archive_evidence_work_item_in_transaction,
    new_recall_selected_archive_evidence_work_item_id,
    suspend_recall_selected_archive_evidence_replay_in_transaction,
)
from friday.interaction_control_plane.selected_archive_evidence import (
    SelectedArchiveCorpus,
    SelectedArchiveCoverageGrade,
    SelectedArchiveEvidence,
)
from friday.interaction_control_plane.work_item_contract import WorkState, WorkTransition
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
    ArchiveSearchCoverageGrade,
    ArchiveSearchSelectedEvidence,
)
from friday.retrieval.archive_search_contract import ArchiveSearchCorpus
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    EmbeddingCompatibility,
    EmbeddingIdentity,
    PassageRef,
    RepresentationKind,
    RevisionKind,
    SourceKind,
    SourceRef,
    SourceRepresentation,
    SourceRevision,
    TextSpanLocator,
)
from friday.storage._archive_search_documents import PASSAGE_INDEX_VERSION

_NOW = "2026-08-24T02:00:00+00:00"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _selected(
    *,
    work_item_id: str,
    boundary_id: str,
    owner: str,
    grade: SelectedArchiveCoverageGrade = SelectedArchiveCoverageGrade.COMPLETE,
) -> tuple[SelectedArchiveEvidence, ArchiveSearchSelectedEvidence]:
    raw_id = "raw_0123456789abcdef"
    source = SourceRef(
        SourceKind.DOCUMENT,
        AuthorityScope.TENANT_PRINCIPAL,
        owner,
        owner,
        CanonicalObjectKind.RAW_OBJECT,
        raw_id,
    )
    revision = SourceRevision(
        SourceRepresentation(RepresentationKind.RAW_OBJECT, raw_id),
        RevisionKind.RAW_CONTENT_SHA256,
        "a" * 64,
    )
    passage = PassageRef(
        source,
        revision,
        TextSpanLocator(chunk_index=0, start_char=0, end_char=28),
        PASSAGE_INDEX_VERSION,
        EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
    )
    evidence = SelectedArchiveEvidence(
        work_item_id=work_item_id,
        corpus=SelectedArchiveCorpus.DOCUMENTS,
        source_ref=source,
        passage_refs=(passage,),
        source_snapshot_sha256="b" * 64,
        coverage_sha256="c" * 64,
        coverage_grade=grade,
        origin_boundary_user_message_id=boundary_id,
    )
    return evidence, ArchiveSearchSelectedEvidence(
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        source_ref=source,
        passage_refs=(passage,),
        resolved_snapshot_sha256=evidence.source_snapshot_sha256,
    )


def _metadata(outcome: ArchiveRecallOutcome) -> tuple[dict[str, Any], str]:
    metadata: dict[str, Any] = {"structural": {"answer_present": True}}
    receipt = attach_accepted_archive_recall_outcome_receipt(metadata, outcome)
    return metadata, receipt.outcome_sha256


def _initial_outcome(
    *,
    answer: str,
    selected: ArchiveSearchSelectedEvidence,
    evidence: SelectedArchiveEvidence,
) -> ArchiveRecallOutcome:
    grade = ArchiveSearchCoverageGrade(evidence.coverage_grade.value)
    return ArchiveRecallOutcome(
        lane=ArchiveRecallLane.FEDERATED_SEARCH,
        status=(
            ArchiveRecallStatus.COMPLETE
            if grade is ArchiveSearchCoverageGrade.COMPLETE
            else ArchiveRecallStatus.PARTIAL
        ),
        plan_sha256="d" * 64,
        evidence_sha256="e" * 64,
        coverage_sha256=evidence.coverage_sha256,
        coverage_grade=grade,
        candidate_count=1,
        used_citation_labels=("A1",),
        selected_evidence=selected,
        publication_attested=True,
        semantic_verified=False,
        answer_sha256=_sha(answer),
    )


def _replay_plan(request: str, selected: ArchiveSearchSelectedEvidence) -> str:
    material = json.dumps(
        {
            "request_sha256": _sha(request),
            "schema": "friday.selected-archive-evidence-replay-plan.v1",
            "selected_evidence_sha256": hashlib.sha256(
                selected.to_private_json().encode("ascii")
            ).hexdigest(),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(material.encode("ascii")).hexdigest()


def _replay_outcome(
    *,
    request: str,
    answer: str,
    selected: ArchiveSearchSelectedEvidence,
    evidence: SelectedArchiveEvidence,
    status: ArchiveRecallStatus,
) -> ArchiveRecallOutcome:
    source_bearing = status in {ArchiveRecallStatus.COMPLETE, ArchiveRecallStatus.PARTIAL}
    return ArchiveRecallOutcome(
        lane=ArchiveRecallLane.SELECTED_EVIDENCE_REPLAY,
        status=status,
        plan_sha256=_replay_plan(request, selected),
        evidence_sha256="f" * 64,
        coverage_sha256=evidence.coverage_sha256,
        coverage_grade=ArchiveSearchCoverageGrade(evidence.coverage_grade.value),
        candidate_count=1 if source_bearing else 0,
        used_citation_labels=("A1.1",) if source_bearing else (),
        selected_evidence=selected if source_bearing else None,
        publication_attested=True,
        semantic_verified=source_bearing,
        answer_sha256=_sha(answer),
    )


def _create_work(
    storage: Any,
    owner: str,
    *,
    grade: SelectedArchiveCoverageGrade = SelectedArchiveCoverageGrade.COMPLETE,
) -> tuple[Any, ArchiveSearchSelectedEvidence]:
    storage.ensure_user(owner, source="local")
    conversation = storage.create_conversation(owner, "Selected archive evidence")
    boundary = storage.store_message(conversation["id"], owner, "user", "Найди точный факт")
    identifier = new_recall_selected_archive_evidence_work_item_id()
    evidence, selected = _selected(
        work_item_id=identifier,
        boundary_id=boundary["id"],
        owner=owner,
        grade=grade,
    )
    answer = "Факт из источника [A1]"
    outcome = _initial_outcome(answer=answer, selected=selected, evidence=evidence)
    metadata, outcome_sha256 = _metadata(outcome)
    assistant = storage.store_message(
        conversation["id"],
        owner,
        "assistant",
        answer,
        metadata=metadata,
        reply_to=boundary["id"],
    )
    with storage.transaction() as conn:
        item = create_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            user_id=owner,
            conversation_id=conversation["id"],
            selected_evidence=evidence,
            anchor_user_message_id=boundary["id"],
            anchor_assistant_message_id=assistant["id"],
            accepted_plan_sha256=outcome.plan_sha256,
            accepted_outcome_sha256=outcome_sha256,
            now=_NOW,
        )
    return item, selected


def _publish_replay(
    storage: Any,
    item: Any,
    selected: ArchiveSearchSelectedEvidence,
    *,
    status: ArchiveRecallStatus,
    request: str = "Что в нём сказано?",
) -> tuple[dict[str, Any], dict[str, Any], ArchiveRecallOutcome, str]:
    boundary = storage.store_message(
        item.conversation_id,
        item.user_id,
        "user",
        request,
        reply_to=item.anchor_assistant_message_id,
    )
    answer = (
        "В выбранном источнике:\n\n[A1.1] Точный фрагмент"
        if status in {ArchiveRecallStatus.COMPLETE, ArchiveRecallStatus.PARTIAL}
        else "Не могу безопасно перечитать выбранный источник."
    )
    outcome = _replay_outcome(
        request=request,
        answer=answer,
        selected=selected,
        evidence=item.selected_evidence,
        status=status,
    )
    metadata, outcome_sha256 = _metadata(outcome)
    assistant = storage.store_message(
        item.conversation_id,
        item.user_id,
        "assistant",
        answer,
        metadata=metadata,
        reply_to=boundary["id"],
    )
    return boundary, assistant, outcome, outcome_sha256


def test_create_get_current_and_sidecar_are_atomic_and_body_free(storage: Any) -> None:
    item, _selected_evidence = _create_work(storage, "archive-store-owner")

    with storage.transaction() as conn:
        loaded = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
        )
        current = get_current_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            now=_NOW,
        )

    assert loaded == current == item
    assert item.state is WorkState.ACTIVE
    assert item.transition is WorkTransition.CREATED
    rows = storage.execute(
        """SELECT work.active_frame_json,evidence.source_ref_json,
                  evidence.passage_refs_json
             FROM work_items work JOIN work_item_selected_evidence evidence
               ON evidence.work_item_id=work.id WHERE work.id=?""",
        (item.id,),
    ).fetchone()
    assert rows is not None
    durable = " ".join(str(value) for value in rows)
    assert all(
        forbidden not in durable
        for forbidden in ("Найди", "Факт", "excerpt", "filename", "query", "model_prose")
    )


def test_create_rejects_receipt_or_selected_evidence_mismatch_atomically(storage: Any) -> None:
    owner = "archive-create-tamper"
    storage.ensure_user(owner, source="local")
    conversation = storage.create_conversation(owner, "tamper")
    boundary = storage.store_message(conversation["id"], owner, "user", "Найди")
    identifier = new_recall_selected_archive_evidence_work_item_id()
    evidence, selected = _selected(
        work_item_id=identifier,
        boundary_id=boundary["id"],
        owner=owner,
    )
    answer = "Ответ [A1]"
    outcome = _initial_outcome(answer=answer, selected=selected, evidence=evidence)
    metadata, outcome_sha256 = _metadata(outcome)
    assistant = storage.store_message(
        conversation["id"], owner, "assistant", answer, metadata=metadata, reply_to=boundary["id"]
    )

    with pytest.raises(WorkItemAnchorError, match="does not match"), storage.transaction() as conn:
        create_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            user_id=owner,
            conversation_id=conversation["id"],
            selected_evidence=evidence,
            anchor_user_message_id=boundary["id"],
            anchor_assistant_message_id=assistant["id"],
            accepted_plan_sha256="0" * 64,
            accepted_outcome_sha256=outcome_sha256,
            now=_NOW,
        )
    assert storage.execute("SELECT 1 FROM work_items WHERE id=?", (identifier,)).fetchone() is None


def test_exact_replay_reanchors_refreshes_ttl_and_supports_next_immediate_followup(
    storage: Any,
) -> None:
    item, selected = _create_work(storage, "archive-replay-owner")
    boundary, assistant, outcome, outcome_sha256 = _publish_replay(
        storage,
        item,
        selected,
        status=ArchiveRecallStatus.COMPLETE,
    )
    replay_now = "2026-08-24T03:00:00+00:00"
    with storage.transaction() as conn:
        replayed = accept_recall_selected_archive_evidence_replay_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=item.revision,
            new_boundary_user_message_id=boundary["id"],
            new_assistant_message_id=assistant["id"],
            new_accepted_plan_sha256=outcome.plan_sha256,
            new_accepted_outcome_sha256=outcome_sha256,
            now=replay_now,
        )

    assert replayed.state is WorkState.ACTIVE
    assert replayed.transition is WorkTransition.EVIDENCE_REPLAYED
    assert replayed.revision == item.revision + 1
    assert replayed.anchor_assistant_message_id == assistant["id"]
    assert replayed.expires_at == (datetime.fromisoformat(replay_now) + timedelta(hours=12)).isoformat(
        timespec="seconds"
    )

    next_boundary = storage.store_message(
        item.conversation_id,
        item.user_id,
        "user",
        "Покажи фрагмент",
        reply_to=assistant["id"],
    )
    with storage.transaction() as conn:
        current = get_current_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            boundary_user_message_id=next_boundary["id"],
            now=replay_now,
        )
    assert current == replayed


def test_replay_requires_current_revision_and_exact_immediate_anchor(storage: Any) -> None:
    item, selected = _create_work(storage, "archive-replay-cas")
    boundary, assistant, outcome, outcome_sha256 = _publish_replay(
        storage,
        item,
        selected,
        status=ArchiveRecallStatus.COMPLETE,
    )
    with pytest.raises(WorkItemConflictError, match="revision"), storage.transaction() as conn:
        accept_recall_selected_archive_evidence_replay_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=item.revision + 1,
            new_boundary_user_message_id=boundary["id"],
            new_assistant_message_id=assistant["id"],
            new_accepted_plan_sha256=outcome.plan_sha256,
            new_accepted_outcome_sha256=outcome_sha256,
            now="2026-08-24T03:00:00+00:00",
        )


@pytest.mark.parametrize(
    "status",
    [
        ArchiveRecallStatus.DENIED,
        ArchiveRecallStatus.DRIFTED,
        ArchiveRecallStatus.UNAVAILABLE,
    ],
)
def test_source_free_replay_failures_reanchor_and_suspend(
    storage: Any,
    status: ArchiveRecallStatus,
) -> None:
    item, selected = _create_work(storage, f"archive-{status.value}")
    boundary, assistant, outcome, outcome_sha256 = _publish_replay(
        storage,
        item,
        selected,
        status=status,
    )
    with storage.transaction() as conn:
        suspended = suspend_recall_selected_archive_evidence_replay_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=item.revision,
            new_boundary_user_message_id=boundary["id"],
            new_assistant_message_id=assistant["id"],
            new_accepted_plan_sha256=outcome.plan_sha256,
            new_accepted_outcome_sha256=outcome_sha256,
            now="2026-08-24T03:00:00+00:00",
        )
        current = get_current_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            now="2026-08-24T03:00:00+00:00",
        )
        loaded = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
        )

    assert current is None
    assert loaded == suspended
    assert suspended.state is WorkState.SUSPENDED
    assert suspended.transition is WorkTransition.SUSPENDED
    assert suspended.anchor_assistant_message_id == assistant["id"]


def test_source_free_replay_plan_still_binds_the_immutable_sidecar(storage: Any) -> None:
    item, selected = _create_work(storage, "archive-sidecar-binding")
    boundary, assistant, outcome, outcome_sha256 = _publish_replay(
        storage,
        item,
        selected,
        status=ArchiveRecallStatus.DRIFTED,
    )
    with storage.transaction() as conn:
        suspended = suspend_recall_selected_archive_evidence_replay_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=item.revision,
            new_boundary_user_message_id=boundary["id"],
            new_assistant_message_id=assistant["id"],
            new_accepted_plan_sha256=outcome.plan_sha256,
            new_accepted_outcome_sha256=outcome_sha256,
            now="2026-08-24T03:00:00+00:00",
        )
        conn.execute("DROP TRIGGER trg_work_item_selected_evidence_immutable")
        conn.execute(
            "UPDATE work_item_selected_evidence SET source_snapshot_sha256=? WHERE work_item_id=?",
            ("9" * 64, item.id),
        )

    with storage.transaction() as conn, pytest.raises(WorkItemAnchorError, match="replay plan"):
        get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=suspended.id,
            user_id=suspended.user_id,
            conversation_id=suspended.conversation_id,
        )


def test_single_and_bulk_expiry_are_kind_scoped(storage: Any) -> None:
    first, _selected_one = _create_work(storage, "archive-expire-one")
    second, _selected_two = _create_work(storage, "archive-expire-two")
    due = "2026-08-24T14:00:01+00:00"
    with storage.transaction() as conn:
        expired = expire_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=first.id,
            user_id=first.user_id,
            conversation_id=first.conversation_id,
            expected_revision=first.revision,
            now=due,
        )
        count = expire_due_recall_selected_archive_evidence_work_items_in_transaction(
            conn,
            user_id=second.user_id,
            now=due,
        )

    assert expired.state is WorkState.EXPIRED
    assert expired.transition is WorkTransition.EXPIRED
    assert count == 1
    row = storage.execute("SELECT state,transition FROM work_items WHERE id=?", (second.id,)).fetchone()
    assert tuple(row) == ("expired", "expired")


def test_disabled_owner_export_getter_accepts_archive_item(storage: Any) -> None:
    item, _selected_evidence = _create_work(storage, "archive-export-owner")
    storage.update_user(item.user_id, status="disabled")

    with storage.transaction() as conn:
        with pytest.raises(WorkItemAnchorError, match="owned and exact"):
            get_recall_selected_archive_evidence_work_item_in_transaction(
                conn,
                work_item_id=item.id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
            )
        exported = get_recall_selected_archive_evidence_work_item_for_export_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
        )

    assert exported == item
    export = storage.export_user(item.user_id)
    payload = json.loads(Path(export["path"]).read_text(encoding="utf-8"))
    assert payload["work_items"] == [item.to_payload()]


def test_creation_final_recheck_rolls_back_late_same_transaction_message(storage: Any) -> None:
    owner = "archive-final-recheck"
    storage.ensure_user(owner, source="local")
    conversation = storage.create_conversation(owner, "late")
    boundary = storage.store_message(conversation["id"], owner, "user", "Найди")
    identifier = new_recall_selected_archive_evidence_work_item_id()
    evidence, selected = _selected(
        work_item_id=identifier,
        boundary_id=boundary["id"],
        owner=owner,
    )
    answer = "Ответ [A1]"
    outcome = _initial_outcome(answer=answer, selected=selected, evidence=evidence)
    metadata, outcome_sha256 = _metadata(outcome)
    assistant = storage.store_message(
        conversation["id"], owner, "assistant", answer, metadata=metadata, reply_to=boundary["id"]
    )

    with pytest.raises(WorkItemAnchorError, match="latest"), storage.transaction() as conn:
        conn.execute(
            """CREATE TEMP TRIGGER archive_work_late_message
               AFTER INSERT ON work_item_selected_evidence BEGIN
                 INSERT INTO messages(
                     id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
                 ) VALUES(
                     'msg_eeeeeeeeeeeeeeee',
                     (SELECT conversation_id FROM work_items WHERE id=NEW.work_item_id),
                     (SELECT user_id FROM work_items WHERE id=NEW.work_item_id),
                     'user','LATE','{}',NULL,'2026-08-24T02:00:01+00:00'
                 );
               END"""
        )
        create_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            user_id=owner,
            conversation_id=conversation["id"],
            selected_evidence=evidence,
            anchor_user_message_id=boundary["id"],
            anchor_assistant_message_id=assistant["id"],
            accepted_plan_sha256=outcome.plan_sha256,
            accepted_outcome_sha256=outcome_sha256,
            now=_NOW,
        )

    assert storage.execute("SELECT 1 FROM work_items WHERE id=?", (identifier,)).fetchone() is None
    assert storage.execute("SELECT 1 FROM messages WHERE id='msg_eeeeeeeeeeeeeeee'").fetchone() is None


def test_store_requires_a_caller_owned_transaction(storage: Any) -> None:
    with pytest.raises(RuntimeError, match="existing transaction"):
        expire_due_recall_selected_archive_evidence_work_items_in_transaction(
            storage.conn,
            now=_NOW,
        )


def test_sidecar_replacement_cannot_detach_a_source_bearing_anchor(storage: Any) -> None:
    item, _selected_evidence = _create_work(storage, "archive-sidecar-source")
    with storage.transaction() as conn:
        conn.execute("DROP TRIGGER trg_work_item_selected_evidence_immutable")
        conn.execute(
            "UPDATE work_item_selected_evidence SET source_snapshot_sha256=? WHERE work_item_id=?",
            ("8" * 64, item.id),
        )
    with (
        storage.transaction() as conn,
        pytest.raises(
            WorkItemAnchorError,
            match="selected evidence changed",
        ),
    ):
        get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
        )


def test_sidecar_work_item_id_is_the_only_creation_identifier(storage: Any) -> None:
    item, _selected_evidence = _create_work(storage, "archive-id-owner")
    assert item.id == item.selected_evidence.work_item_id
    assert item.id.startswith("work_") and len(item.id) == 21


def test_creation_rejects_origin_boundary_mismatch_before_storage(storage: Any) -> None:
    item, _selected_evidence = _create_work(storage, "archive-origin-base")
    boundary = storage.store_message(
        item.conversation_id,
        item.user_id,
        "user",
        "Новый поиск",
    )
    identifier = new_recall_selected_archive_evidence_work_item_id()
    evidence, _unused_selected = _selected(
        work_item_id=identifier,
        boundary_id=boundary["id"],
        owner=item.user_id,
    )
    mismatched = replace(evidence, origin_boundary_user_message_id=item.anchor_user_message_id)
    with pytest.raises(WorkItemAnchorError, match="origin"), storage.transaction() as conn:
        create_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            selected_evidence=mismatched,
            anchor_user_message_id=boundary["id"],
            anchor_assistant_message_id=item.anchor_assistant_message_id,
            accepted_plan_sha256=item.accepted_plan_sha256,
            accepted_outcome_sha256=item.accepted_outcome_sha256,
            now=_NOW,
        )
