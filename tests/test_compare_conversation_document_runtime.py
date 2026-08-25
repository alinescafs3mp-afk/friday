from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from test_compare_conversation_document_schema42 import (  # noqa: PLC2701
    _NOW,
    _RESOLVED_AT,
    _archive_metadata,
    _create_writer_followup_waiting,
    _prepare_writer_document,
    _selected_messages,
)
from test_conversation_document_comparison import _ComparisonModel  # noqa: PLC2701

import friday.orchestration.conversation_document_comparison as comparison_module
from friday.agent_runtime import AgentRuntime
from friday.file_evidence import stamp_current_turn_file_reference
from friday.interaction_control_plane.archive_evidence_work_item_store import (
    create_recall_selected_archive_evidence_work_item_in_transaction,
    get_recall_selected_archive_evidence_work_item_in_transaction,
)
from friday.interaction_control_plane.compare_conversation_document_store import (
    get_compare_conversation_with_document_work_item_in_transaction,
    get_current_compare_conversation_with_document_work_item_in_transaction,
    resolve_compare_conversation_document_reference_in_transaction,
    suspend_compare_conversation_with_document_in_transaction,
)
from friday.interaction_control_plane.work_item_contract import WorkState
from friday.interaction_control_plane.work_item_store import WorkItemConflictError
from friday.permissions import AuthorizationService
from friday.retrieval.archive_evidence_replay import (
    ArchiveEvidenceReplayCoverageGrade,
    _exact_result,  # noqa: PLC2701 - focused process-private runtime fixture
)
from friday.retrieval.archive_search_contract import ArchiveSearchCorpus
from friday.retrieval.contracts import (
    LifecycleRef,
    LifecycleState,
    ResolvedSource,
    RevalidationTarget,
)
from friday.storage.models import RawObject, new_id


def _register_exact_text_document(
    storage: Any,
    settings: Any,
    *,
    owner: str,
    filename: str,
) -> RawObject:
    text = "В документе выбран обычный режим без CUDA graphs."
    content = text.encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    relative = f"{owner}/{digest[:2]}/{digest}.bin"
    target = Path(settings.files_dir) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    raw = RawObject(
        id=new_id("raw"),
        user_id=owner,
        source="upload",
        source_ref=f"telegram-file:{new_id('source')}",
        raw_content=text,
        content_type="file",
        content_hash=digest,
        metadata_json={
            "filename": filename,
            "mime_type": "text/plain",
            "stored_path": relative,
            "sha256": digest,
            "size_bytes": len(content),
            "uploaded_by": owner,
            "extraction_receipt_version": 1,
            "extraction_success": True,
            "extraction_error": "",
            "text_extraction_success": True,
            "text_sha256": hashlib.sha256(" ".join(text.split()).encode()).hexdigest(),
            "extraction_chars": len(text),
            "text_truncated": False,
            "archive_truncated": False,
            "source_truncated_for_parse": False,
            "parse_deadline_reached": False,
            "parse_pages_read": 0,
            "parse_pages_truncated": False,
            "parse_total_pages": 0,
            "vision_pages_total": 0,
            "vision_pages_read": 0,
            "archive_files": 0,
            "archive_files_read": 0,
            "vision_used": False,
            "vision_review_required": False,
            "unsupported_format": False,
        },
    )
    storage.store_raw_object(raw)
    with storage.transaction() as conn:
        if (
            conn.execute(
                "SELECT 1 FROM file_source_aliases WHERE user_id=? AND uploaded_by=? AND raw_object_id=?",
                (owner, owner, raw.id),
            ).fetchone()
            is None
        ):
            conn.execute(
                """INSERT INTO file_source_aliases(
                       user_id,uploaded_by,source_ref,raw_object_id,supplied_filename,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (owner, owner, raw.source_ref, raw.id, filename, "2026-08-25T08:05:00+00:00"),
            )
    return raw


def _exact_replay(item: Any) -> Any:
    evidence = item.selected_message_evidence
    passage = evidence.passage_refs[0]
    representation = passage.source_revision.representation
    resolved = ResolvedSource.create(
        source_ref=evidence.source_ref,
        representations=(representation,),
        lifecycle=(LifecycleRef(representation, LifecycleState.ACTIVE),),
        revisions=(passage.source_revision,),
        revalidation_targets=(RevalidationTarget(representation, evidence.source_ref.authority_scope),),
    )
    return _exact_result(
        corpus=ArchiveSearchCorpus.MESSAGES,
        coverage_grade=ArchiveEvidenceReplayCoverageGrade.COMPLETE,
        resolved_source=resolved,
        passage_refs=(passage,),
        texts=("В переписке решили оставить точный режим CUDA graphs.",),
    )


@pytest.mark.asyncio
async def test_selected_message_followup_atomically_creates_q1_and_suspends_old_selection(
    settings: Any,
    storage: Any,
) -> None:
    owner = "compare-runtime-initial-owner"
    storage.ensure_user(owner, preset_key="owner")
    conversation = storage.create_conversation(owner, "runtime initial comparison")
    origin_boundary = storage.store_message(conversation["id"], owner, "user", "find exact messages")
    selected_id = new_id("work")
    selected_evidence, selected_projection = _selected_messages(
        storage,
        owner=owner,
        work_item_id=selected_id,
        origin_boundary_id=origin_boundary["id"],
    )
    archive_answer = "Exact selected messages [A1.1]."
    metadata, outcome, outcome_sha256 = _archive_metadata(
        answer=archive_answer,
        selected=selected_projection,
        evidence=selected_evidence,
    )
    origin_assistant = storage.store_message(
        conversation["id"],
        owner,
        "assistant",
        archive_answer,
        metadata=metadata,
        reply_to=origin_boundary["id"],
    )
    with storage.transaction() as conn:
        selected = create_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            user_id=owner,
            conversation_id=conversation["id"],
            selected_evidence=selected_evidence,
            anchor_user_message_id=origin_boundary["id"],
            anchor_assistant_message_id=origin_assistant["id"],
            accepted_plan_sha256=outcome.plan_sha256,
            accepted_outcome_sha256=outcome_sha256,
            now=_NOW,
        )
    actor = AuthorizationService(storage).actor_for_user(owner, source="comparison-runtime-test")
    runtime = AgentRuntime(settings, storage)

    result = await runtime.chat(
        owner,
        "Сравни выбранные сообщения с документом.",
        actor=actor,
        conversation_id=conversation["id"],
    )

    assert result["context"]["compare_conversation_with_document"] == "waiting_for_input"
    with storage.transaction() as conn:
        old = get_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            work_item_id=selected.id,
            user_id=owner,
            conversation_id=conversation["id"],
        )
        current = get_current_compare_conversation_with_document_work_item_in_transaction(
            conn,
            user_id=owner,
            conversation_id=conversation["id"],
        )
        assert old is not None
        assert old.state is WorkState.SUSPENDED
        assert current is not None
        assert current.state is WorkState.WAITING_FOR_INPUT
        rows = conn.execute(
            "SELECT role FROM messages WHERE user_id=? AND conversation_id=? ORDER BY rowid",
            (owner, conversation["id"]),
        ).fetchall()
    assert [row[0] for row in rows[-2:]] == ["user", "assistant"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_mode", ["exact_filename", "current_attachment"])
async def test_active_comparison_restarts_without_an_intervening_ordinary_row(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    reference_mode: str,
) -> None:
    owner = "compare-writer-owner"
    conversation, waiting, _evidence = _create_writer_followup_waiting(storage, owner=owner)
    filename = "restart-decision.txt"
    raw = _register_exact_text_document(storage, settings, owner=owner, filename=filename)
    attachments: list[dict[str, Any]] | None = None
    if reference_mode == "current_attachment":

        class Carrier(dict[str, Any]):
            pass

        carrier = Carrier(raw_object_id=raw.id, filename=filename, mime_type="text/plain")
        stamp_current_turn_file_reference(carrier, raw.to_row())
        attachments = [carrier]
    actor = AuthorizationService(storage).actor_for_user(owner, source="comparison-runtime-test")
    first_runtime = AgentRuntime(settings, storage, selected_archive_model=_ComparisonModel())

    class SimulatedRestart(RuntimeError):
        pass

    async def stop_after_active(**_kwargs: Any) -> dict[str, Any]:
        raise SimulatedRestart

    monkeypatch.setattr(first_runtime, "_execute_active_conversation_document_comparison", stop_after_active)
    with pytest.raises(SimulatedRestart):
        await first_runtime.chat(
            owner,
            filename,
            actor=actor,
            conversation_id=conversation["id"],
            attachments=attachments,
        )

    with storage.transaction() as conn:
        active = get_current_compare_conversation_with_document_work_item_in_transaction(
            conn,
            user_id=owner,
            conversation_id=conversation["id"],
        )
        assert active is not None
        assert active.id == waiting.id
        assert active.state is WorkState.ACTIVE
        rows_before_restart = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE user_id=? AND conversation_id=?",
            (owner, conversation["id"]),
        ).fetchone()[0]

    replay = _exact_replay(active)
    monkeypatch.setattr(
        comparison_module,
        "archive_selected_evidence_snapshot_sha256",
        lambda *_args, **_kwargs: active.selected_message_evidence.source_snapshot_sha256,
    )
    restarted = AgentRuntime(settings, storage, selected_archive_model=_ComparisonModel())
    monkeypatch.setattr(
        restarted,
        "_comparison_replay_in_transaction",
        lambda *_args, **_kwargs: replay,
    )
    result = await restarted.chat(
        owner,
        "продолжи",
        actor=actor,
        conversation_id=conversation["id"],
    )

    assert result["verified"] is True
    assert result["tools_used"] == []
    with storage.transaction() as conn:
        completed = get_compare_conversation_with_document_work_item_in_transaction(
            conn,
            work_item_id=active.id,
            user_id=owner,
            conversation_id=conversation["id"],
        )
        assert completed is not None
        assert completed.state is WorkState.COMPLETED
        rows = conn.execute(
            "SELECT role,content FROM messages WHERE user_id=? AND conversation_id=? ORDER BY rowid",
            (owner, conversation["id"]),
        ).fetchall()
    assert len(rows) == rows_before_restart + 1
    assert not any(row[0] == "user" and row[1] == "продолжи" for row in rows)


def test_source_drift_can_only_cas_active_comparison_to_suspended(storage: Any) -> None:
    conversation, waiting, _evidence = _create_writer_followup_waiting(storage)
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
            expected_revision=waiting.revision,
            boundary_user_message_id=boundary["id"],
            document_evidence=document,
            now=_RESOLVED_AT,
        )
        conn.execute("UPDATE raw_objects SET content_hash=? WHERE id=?", ("f" * 64, document.raw_object_id))
        suspended = suspend_compare_conversation_with_document_in_transaction(
            conn,
            work_item_id=active.id,
            user_id=active.user_id,
            conversation_id=active.conversation_id,
            expected_revision=active.revision,
        )
    assert suspended.state is WorkState.SUSPENDED
    with storage.transaction() as conn, pytest.raises(WorkItemConflictError):
        suspend_compare_conversation_with_document_in_transaction(
            conn,
            work_item_id=active.id,
            user_id=active.user_id,
            conversation_id=active.conversation_id,
            expected_revision=active.revision,
        )
