from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

from friday.account_deletion import _mark_account_deletion_history_clean, preflight_account_deletion
from friday.interaction_control_plane.archive_evidence_work_item import (
    RecallSelectedArchiveEvidenceActiveFrame,
)
from friday.interaction_control_plane.archive_evidence_work_item_store import (
    create_recall_selected_archive_evidence_work_item_in_transaction,
)
from friday.interaction_control_plane.selected_archive_evidence import (
    SelectedArchiveCorpus,
    SelectedArchiveCoverageGrade,
    SelectedArchiveEvidence,
)
from friday.interaction_control_plane.work_item_schema import (
    WORK_ITEM_SCHEMA,
    _execute_schema,
    validate_work_item_schema,
)
from friday.interaction_control_plane.work_item_store import (
    WorkItemAnchorError,
    expire_due_recall_conversation_work_items_in_transaction,
    get_current_recall_conversation_work_item_in_transaction,
)
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
from friday.storage.models import new_id

_NOW = "2026-08-23T08:00:00+00:00"
_EXPIRES = "2026-08-23T20:00:00+00:00"


def _selected_document(
    *,
    work_item_id: str,
    boundary_id: str,
    owner: str,
    corpus: SelectedArchiveCorpus = SelectedArchiveCorpus.DOCUMENTS,
    source_kind: SourceKind = SourceKind.DOCUMENT,
) -> SelectedArchiveEvidence:
    raw_id = "raw_0123456789abcdef"
    source = SourceRef(
        source_kind,
        AuthorityScope.TENANT_PRINCIPAL,
        owner,
        owner,
        CanonicalObjectKind.RAW_OBJECT,
        raw_id,
    )
    if corpus is SelectedArchiveCorpus.KNOWLEDGE:
        representation = SourceRepresentation(
            RepresentationKind.KNOWLEDGE_OBJECT,
            "ko_0123456789abcdef",
        )
        revision = SourceRevision(representation, RevisionKind.KNOWLEDGE_VERSION, "1")
        passage_index_version = PASSAGE_INDEX_VERSION
    else:
        representation = SourceRepresentation(RepresentationKind.RAW_OBJECT, raw_id)
        revision = SourceRevision(representation, RevisionKind.RAW_CONTENT_SHA256, "a" * 64)
        passage_index_version = PASSAGE_INDEX_VERSION
    passage = PassageRef(
        source,
        revision,
        TextSpanLocator(chunk_index=0, start_char=10, end_char=40),
        passage_index_version,
        EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
    )
    return SelectedArchiveEvidence(
        work_item_id=work_item_id,
        corpus=corpus,
        source_ref=source,
        passage_refs=(passage,),
        source_snapshot_sha256="b" * 64,
        coverage_sha256="c" * 64,
        coverage_grade=SelectedArchiveCoverageGrade.COMPLETE,
        origin_boundary_user_message_id=boundary_id,
    )


def _seed_archive_work_item(
    storage,
    owner: str,
    *,
    insert_evidence: bool = True,
    transition: str = "created",
    revision: int = 1,
    active_frame_json: str | None = None,
) -> tuple[dict[str, str], SelectedArchiveEvidence]:
    storage.ensure_user(owner, source="local")
    conversation = storage.create_conversation(owner, "Archive evidence")
    boundary = storage.store_message(conversation["id"], owner, "user", "Найди документ")
    assistant = storage.store_message(
        conversation["id"],
        owner,
        "assistant",
        "[A1]",
        reply_to=boundary["id"],
    )
    work_item_id = new_id("work")
    evidence = _selected_document(
        work_item_id=work_item_id,
        boundary_id=boundary["id"],
        owner=owner,
    )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO work_items(
                   id,user_id,conversation_id,kind,goal,state,playbook,
                   completion_contract,active_frame_json,anchor_user_message_id,
                   anchor_assistant_message_id,accepted_plan_sha256,
                   accepted_outcome_sha256,revision,transition,created_at,
                   updated_at,expires_at,closed_at
               ) VALUES(?,?,?,'recall_selected_archive_evidence',
                        'exact_selected_archive_evidence_recall','active',
                        'recall_selected_archive_evidence',
                        'accepted_exact_selected_archive_evidence',
                        ?,?,?,?,?,?,?,?,?,?,NULL)""",
            (
                work_item_id,
                owner,
                conversation["id"],
                (
                    active_frame_json
                    if active_frame_json is not None
                    else RecallSelectedArchiveEvidenceActiveFrame().to_json()
                ),
                boundary["id"],
                assistant["id"],
                "d" * 64,
                "e" * 64,
                revision,
                transition,
                _NOW,
                _NOW,
                _EXPIRES,
            ),
        )
        if insert_evidence:
            payload = evidence.to_storage_payload()
            conn.execute(
                """INSERT INTO work_item_selected_evidence(
                       work_item_id,corpus,source_ref_json,passage_refs_json,
                       source_snapshot_sha256,coverage_sha256,coverage_grade,
                       origin_boundary_user_message_id
                   ) VALUES(:work_item_id,:corpus,:source_ref_json,:passage_refs_json,
                            :source_snapshot_sha256,:coverage_sha256,:coverage_grade,
                            :origin_boundary_user_message_id)""",
                payload,
            )
    return {
        "id": work_item_id,
        "owner": owner,
        "conversation_id": conversation["id"],
        "boundary_id": boundary["id"],
        "assistant_id": assistant["id"],
    }, evidence


def test_selected_evidence_is_one_body_free_row_and_cascades_with_parent(storage) -> None:
    work, evidence = _seed_archive_work_item(storage, "sidecar-owner")

    stored = storage.execute(
        "SELECT * FROM work_item_selected_evidence WHERE work_item_id=?",
        (work["id"],),
    ).fetchone()
    assert stored is not None
    assert SelectedArchiveEvidence.from_storage_row(dict(stored)) == evidence
    validate_work_item_schema(storage.conn)

    with storage.transaction() as conn:
        conn.execute("DELETE FROM work_items WHERE id=?", (work["id"],))
    assert (
        storage.execute(
            "SELECT 1 FROM work_item_selected_evidence WHERE work_item_id=?",
            (work["id"],),
        ).fetchone()
        is None
    )


@pytest.mark.parametrize("source_kind", [SourceKind.WEB_CAPTURE, SourceKind.GENERATED_ARTIFACT])
def test_knowledge_sidecar_accepts_each_promoted_document_source_kind(storage, source_kind) -> None:
    work, _document = _seed_archive_work_item(
        storage,
        f"promoted-{source_kind.value}",
        insert_evidence=False,
    )
    evidence = _selected_document(
        work_item_id=work["id"],
        boundary_id=work["boundary_id"],
        owner=work["owner"],
        corpus=SelectedArchiveCorpus.KNOWLEDGE,
        source_kind=source_kind,
    )
    with storage.transaction() as conn:
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

    validate_work_item_schema(storage.conn)


def test_archive_workflow_accepts_only_created_or_evidence_replayed(storage) -> None:
    _seed_archive_work_item(storage, "archive-created")
    _seed_archive_work_item(
        storage,
        "archive-replayed",
        transition="evidence_replayed",
        revision=2,
    )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
        _seed_archive_work_item(
            storage,
            "archive-wrong-transition",
            transition="constraint_updated",
            revision=2,
        )


def test_sidecar_scope_requires_archive_kind_and_owned_user_boundary(storage) -> None:
    work, evidence = _seed_archive_work_item(storage, "scope-owner")
    with storage.transaction() as conn:
        conn.execute("DELETE FROM work_items WHERE id=?", (work["id"],))

    storage.ensure_user("recall-owner", source="local")
    conversation = storage.create_conversation("recall-owner", "Recall")
    boundary = storage.store_message(conversation["id"], "recall-owner", "user", "Вчера?")
    assistant = storage.store_message(
        conversation["id"],
        "recall-owner",
        "assistant",
        "Ответ",
        reply_to=boundary["id"],
    )
    recall_id = new_id("work")
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO work_items VALUES(
                   ?,?,?,'recall_conversation','exact_current_conversation_recall',
                   'active','recall_conversation','accepted_exact_owned_message_window',
                   '{}',?,?,?, ?,1,'created',?,?,?,NULL)""",
            (
                recall_id,
                "recall-owner",
                conversation["id"],
                boundary["id"],
                assistant["id"],
                "a" * 64,
                "b" * 64,
                _NOW,
                _NOW,
                _EXPIRES,
            ),
        )
        hostile = {
            **evidence.to_storage_payload(),
            "work_item_id": recall_id,
            "origin_boundary_user_message_id": boundary["id"],
        }
        with pytest.raises(sqlite3.IntegrityError, match="scope"):
            conn.execute(
                """INSERT INTO work_item_selected_evidence VALUES(
                       :work_item_id,:corpus,:source_ref_json,:passage_refs_json,
                       :source_snapshot_sha256,:coverage_sha256,:coverage_grade,
                       :origin_boundary_user_message_id)""",
                hostile,
            )


def test_archive_base_row_requires_the_exact_body_free_active_frame(storage) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
        _seed_archive_work_item(
            storage,
            "invalid-frame-owner",
            active_frame_json="{}",
        )


def test_sidecar_scope_binds_origin_anchor_and_principal_owner(storage) -> None:
    work, evidence = _seed_archive_work_item(storage, "bound-owner", insert_evidence=False)
    later_boundary = storage.store_message(
        work["conversation_id"],
        work["owner"],
        "user",
        "Другой запрос",
    )
    wrong_boundary = replace(
        evidence,
        origin_boundary_user_message_id=later_boundary["id"],
    )
    foreign_source = replace(evidence.source_ref, principal_id="foreign-owner")
    foreign_passages = tuple(replace(passage, source_ref=foreign_source) for passage in evidence.passage_refs)
    foreign_owner = replace(
        evidence,
        source_ref=foreign_source,
        passage_refs=foreign_passages,
    )

    for hostile in (wrong_boundary, foreign_owner):
        with pytest.raises(sqlite3.IntegrityError, match="scope"), storage.transaction() as conn:
            conn.execute(
                """INSERT INTO work_item_selected_evidence(
                       work_item_id,corpus,source_ref_json,passage_refs_json,
                       source_snapshot_sha256,coverage_sha256,coverage_grade,
                       origin_boundary_user_message_id
                   ) VALUES(:work_item_id,:corpus,:source_ref_json,:passage_refs_json,
                            :source_snapshot_sha256,:coverage_sha256,:coverage_grade,
                            :origin_boundary_user_message_id)""",
                hostile.to_storage_payload(),
            )

    with pytest.raises(WorkItemAnchorError, match="not owned"), storage.transaction() as conn:
        create_recall_selected_archive_evidence_work_item_in_transaction(
            conn,
            user_id=work["owner"],
            conversation_id=work["conversation_id"],
            selected_evidence=foreign_owner,
            anchor_user_message_id=work["boundary_id"],
            anchor_assistant_message_id=work["assistant_id"],
            accepted_plan_sha256="d" * 64,
            accepted_outcome_sha256="e" * 64,
        )


def test_current_data_validator_rejects_frame_and_principal_corruption(storage) -> None:
    frame_work, _frame_evidence = _seed_archive_work_item(storage, "frame-corruption")
    with storage.transaction() as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            "UPDATE work_items SET active_frame_json='{}' WHERE id=?",
            (frame_work["id"],),
        )
        conn.execute("PRAGMA ignore_check_constraints=OFF")
    with pytest.raises(sqlite3.DatabaseError, match="archive Work Item data"):
        validate_work_item_schema(storage.conn)

    with storage.transaction() as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            "UPDATE work_items SET active_frame_json=? WHERE id=?",
            (RecallSelectedArchiveEvidenceActiveFrame().to_json(), frame_work["id"]),
        )
        conn.execute("PRAGMA ignore_check_constraints=OFF")

    owner_work, owner_evidence = _seed_archive_work_item(storage, "owner-corruption")
    foreign_source = replace(owner_evidence.source_ref, principal_id="foreign-owner")
    foreign_passages = tuple(
        replace(passage, source_ref=foreign_source) for passage in owner_evidence.passage_refs
    )
    foreign_owner = replace(
        owner_evidence,
        source_ref=foreign_source,
        passage_refs=foreign_passages,
    )
    payload = foreign_owner.to_storage_payload()
    with storage.transaction() as conn:
        conn.execute("DROP TRIGGER trg_work_item_selected_evidence_immutable")
        conn.execute(
            """UPDATE work_item_selected_evidence
                  SET source_ref_json=:source_ref_json,
                      passage_refs_json=:passage_refs_json
                WHERE work_item_id=:work_item_id""",
            payload,
        )
        _execute_schema(conn, WORK_ITEM_SCHEMA)
    with pytest.raises(sqlite3.DatabaseError, match="owner"):
        validate_work_item_schema(storage.conn)


def test_sidecar_primary_key_count_and_json_bounds_fail_closed(storage) -> None:
    work, evidence = _seed_archive_work_item(storage, "bounded-owner")
    payload = evidence.to_storage_payload()
    with storage.transaction() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                """INSERT INTO work_item_selected_evidence VALUES(
                       :work_item_id,:corpus,:source_ref_json,:passage_refs_json,
                       :source_snapshot_sha256,:coverage_sha256,:coverage_grade,
                       :origin_boundary_user_message_id)""",
                payload,
            )
        conn.execute("DELETE FROM work_item_selected_evidence WHERE work_item_id=?", (work["id"],))

    for passages in ("[]", json.dumps([json.loads(evidence.passage_refs[0].to_private_json())] * 9)):
        with (
            pytest.raises(
                sqlite3.IntegrityError,
                match="CHECK constraint",
            ),
            storage.transaction() as conn,
        ):
            conn.execute(
                """INSERT INTO work_item_selected_evidence VALUES(?,?,?,?,?,?,?,?)""",
                (
                    work["id"],
                    "documents",
                    evidence.source_ref.to_private_json(),
                    passages,
                    "a" * 64,
                    "b" * 64,
                    "complete",
                    work["boundary_id"],
                ),
            )


def test_selected_evidence_row_is_immutable(storage) -> None:
    work, _evidence = _seed_archive_work_item(storage, "immutable-owner")

    with pytest.raises(sqlite3.IntegrityError, match="immutable"), storage.transaction() as conn:
        conn.execute(
            "UPDATE work_item_selected_evidence SET coverage_grade='partial' WHERE work_item_id=?",
            (work["id"],),
        )


def test_validator_rejects_archive_item_without_its_exactly_one_sidecar(storage) -> None:
    _seed_archive_work_item(storage, "missing-sidecar", insert_evidence=False)

    with pytest.raises(sqlite3.DatabaseError, match="cardinality"):
        validate_work_item_schema(storage.conn)


def test_validator_rejects_noncanonical_typed_identity_even_when_sql_shape_passes(storage) -> None:
    work, evidence = _seed_archive_work_item(storage, "typed-tamper", insert_evidence=False)
    source = evidence.source_ref.to_private_payload()
    source["body"] = "must never persist"
    source_json = json.dumps(source, sort_keys=True, separators=(",", ":"))
    passage = evidence.passage_refs[0].to_private_payload()
    passage["source_ref"] = source
    passage_json = json.dumps([passage], sort_keys=True, separators=(",", ":"))
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO work_item_selected_evidence VALUES(?,?,?,?,?,?,?,?)""",
            (
                work["id"],
                "documents",
                source_json,
                passage_json,
                "b" * 64,
                "c" * 64,
                "complete",
                work["boundary_id"],
            ),
        )

    with pytest.raises(sqlite3.DatabaseError, match="identity"):
        validate_work_item_schema(storage.conn)


def test_recall_getter_expiry_and_export_fail_closed_around_archive_kind(storage) -> None:
    work, _evidence = _seed_archive_work_item(storage, "kind-dispatch-owner")

    with storage.transaction() as conn:
        assert (
            get_current_recall_conversation_work_item_in_transaction(
                conn,
                user_id=work["owner"],
                conversation_id=work["conversation_id"],
                now=_NOW,
            )
            is None
        )
        assert (
            expire_due_recall_conversation_work_items_in_transaction(
                conn,
                user_id=work["owner"],
                now="2026-08-24T08:00:00+00:00",
            )
            == 0
        )
    row = storage.execute("SELECT state,transition FROM work_items WHERE id=?", (work["id"],)).fetchone()
    assert tuple(row) == ("active", "created")
    assert storage.export_user(work["owner"])["path"]


def test_account_deletion_inventory_owns_selected_evidence_through_work_item(storage) -> None:
    work, _evidence = _seed_archive_work_item(storage, "sidecar-delete-owner")
    assert _mark_account_deletion_history_clean(storage, work["owner"])
    storage.update_user(work["owner"], status="disabled")

    plan = preflight_account_deletion(storage, work["owner"], quiescence_available=True)

    assert plan["counts"]["work_items"] == 1
    assert plan["counts"]["work_item_selected_evidence"] == 1
    assert plan["unknown_scopes"] == []
    assert plan["cross_account_object_references"] == {
        "foreign_keys": {},
        "non_fk": {},
    }
