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
    ARCHIVE_CANDIDATE_REASK_VERDICT_KIND,
    ArchiveCandidateItem,
    ArchiveCandidateSelectionError,
    ArchiveCandidateSet,
    archive_candidate_reask_prompt,
    archive_candidate_selection_offer_suffix,
    parse_archive_candidate_ordinal,
)
from friday.interaction_control_plane.archive_candidate_selection_store import (
    accept_archive_candidate_selection_in_transaction,
    cancel_archive_candidate_selection_in_transaction,
    create_archive_candidate_selection_work_item_in_transaction,
    expire_archive_candidate_selection_in_transaction,
    get_archive_candidate_selection_work_item_in_transaction,
    get_current_archive_candidate_selection_work_item_in_transaction,
    new_archive_candidate_selection_work_item_id,
    new_archive_candidate_set_id,
    reask_archive_candidate_selection_in_transaction,
    suspend_after_replay_failure_in_transaction,
)
from friday.interaction_control_plane.selected_archive_evidence import (
    SelectedArchiveCorpus,
    SelectedArchiveCoverageGrade,
)
from friday.interaction_control_plane.work_item_contract import (
    RECALL_SELECTED_ARCHIVE_EVIDENCE_ACTIVE_FRAME_JSON,
    WorkState,
    WorkTransition,
)
from friday.interaction_control_plane.work_item_schema import (
    _WORK_ITEM_SCHEMA_39,
    WORK_ITEM_SCHEMA,
    _execute_schema,
    upgrade_work_item_schema_to_40,
    validate_work_item_schema,
)
from friday.interaction_control_plane.work_item_store import (
    WorkItemAnchorError,
    WorkItemConflictError,
)
from friday.orchestration.archive_recall_outcome import (
    ARCHIVE_EVIDENCE_REPLAY_UNAVAILABLE,
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
from friday.storage import FridayStorage
from friday.storage._archive_search_documents import PASSAGE_INDEX_VERSION

_NOW = "2026-08-25T05:00:00+00:00"
_AFTER_EXPIRY = "2026-08-25T17:00:01+00:00"
_QUERY_CANARY = "PRIVATE-QUERY-CANARY"
_TITLE_CANARY = "PRIVATE-TITLE-CANARY"
_EXCERPT_CANARY = "PRIVATE-EXCERPT-CANARY"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _candidate(
    *,
    owner: str,
    ordinal: int,
) -> ArchiveCandidateItem:
    raw_id = f"raw_{ordinal:016x}"
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
        f"{ordinal:x}" * 64,
    )
    passage = PassageRef(
        source,
        revision,
        TextSpanLocator(chunk_index=0, start_char=ordinal * 10, end_char=ordinal * 10 + 8),
        PASSAGE_INDEX_VERSION,
        EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
    )
    return ArchiveCandidateItem(
        ordinal=ordinal,
        public_citation_label=f"A{ordinal}",
        corpus=SelectedArchiveCorpus.DOCUMENTS,
        source_ref=source,
        passage_refs=(passage,),
        source_snapshot_sha256=f"{ordinal + 2:x}" * 64,
    )


def _metadata(outcome: ArchiveRecallOutcome) -> tuple[dict[str, Any], str]:
    metadata: dict[str, Any] = {"structural": {"answer_present": True}}
    receipt = attach_accepted_archive_recall_outcome_receipt(metadata, outcome)
    return metadata, receipt.outcome_sha256


def _projection(
    owner: str,
    *,
    reverse_sources: bool = False,
    public_citation_labels: tuple[str, str] = ("A1", "A2"),
) -> ArchiveSearchAcceptedCandidateProjection:
    candidates = (_candidate(owner=owner, ordinal=1), _candidate(owner=owner, ordinal=2))
    ordered = tuple(reversed(candidates)) if reverse_sources else candidates
    return _new_accepted_candidate_projection(
        candidates=tuple(
            ArchiveSearchCandidateProjectionEntry(
                ordinal=ordinal,
                public_citation_label=public_citation_labels[ordinal - 1],
                corpus=ArchiveSearchCorpus(item.corpus.value),
                source_ref=item.source_ref,
                passage_refs=item.passage_refs,
                resolved_snapshot_sha256=item.source_snapshot_sha256,
            )
            for ordinal, item in enumerate(ordered, start=1)
        ),
        coverage_grade=ArchiveSearchCoverageGrade.COMPLETE,
        coverage_sha256="c" * 64,
        evidence_sha256="e" * 64,
    )


def _initial_outcome(
    *,
    answer: str,
    projection: ArchiveSearchAcceptedCandidateProjection,
) -> ArchiveRecallOutcome:
    return ArchiveRecallOutcome(
        lane=ArchiveRecallLane.FEDERATED_SEARCH,
        status=ArchiveRecallStatus.COMPLETE,
        plan_sha256="d" * 64,
        evidence_sha256=projection.evidence_sha256,
        coverage_sha256=projection.coverage_sha256,
        coverage_grade=ArchiveSearchCoverageGrade.COMPLETE,
        candidate_count=projection.candidate_count,
        used_citation_labels=("A1.1", "A2.1"),
        selected_evidence=None,
        publication_attested=True,
        semantic_verified=False,
        answer_sha256=_sha(answer),
        candidate_projection_sha256=projection.canonical_sha256,
    )


def _archive_selection(candidate_set: ArchiveCandidateSet, ordinal: int) -> ArchiveSearchSelectedEvidence:
    evidence = candidate_set.selected_evidence(ordinal)
    return ArchiveSearchSelectedEvidence(
        corpus=ArchiveSearchCorpus(evidence.corpus.value),
        source_ref=evidence.source_ref,
        passage_refs=evidence.passage_refs,
        resolved_snapshot_sha256=evidence.source_snapshot_sha256,
    )


def _replay_plan(request: str, selected: ArchiveSearchSelectedEvidence) -> str:
    encoded = json.dumps(
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
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _create_work(storage: Any, owner: str) -> Any:
    storage.ensure_user(owner, source="local")
    conversation = storage.create_conversation(owner, _TITLE_CANARY)
    boundary = storage.store_message(
        conversation["id"],
        owner,
        "user",
        f"{_QUERY_CANARY}: найди два документа",
    )
    identifier = new_archive_candidate_selection_work_item_id()
    candidate_set_id = new_archive_candidate_set_id()
    projection = _projection(owner)
    answer = (
        f"Сравнение источников [A1.1] и [A2.1]. {_EXCERPT_CANARY}\n\n"
        + archive_candidate_selection_offer_suffix(("A1", "A2"))
    )
    outcome = _initial_outcome(answer=answer, projection=projection)
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
        return create_archive_candidate_selection_work_item_in_transaction(
            conn,
            user_id=owner,
            conversation_id=conversation["id"],
            accepted_candidate_projection=projection,
            work_item_id=identifier,
            candidate_set_id=candidate_set_id,
            anchor_user_message_id=boundary["id"],
            anchor_assistant_message_id=assistant["id"],
            accepted_plan_sha256=outcome.plan_sha256,
            accepted_outcome_sha256=outcome_sha256,
            now=_NOW,
        )


def _publish_replay(storage: Any, item: Any, *, ordinal: int) -> tuple[Any, Any, Any, str]:
    request = "Второй" if ordinal == 2 else "Первый"
    boundary = storage.store_message(
        item.conversation_id,
        item.user_id,
        "user",
        request,
        reply_to=item.question.prompt_assistant_message_id,
    )
    selected = _archive_selection(item.candidate_set, ordinal)
    answer = "Точный фрагмент выбранного источника [A1.1]"
    outcome = ArchiveRecallOutcome(
        lane=ArchiveRecallLane.SELECTED_EVIDENCE_REPLAY,
        status=ArchiveRecallStatus.COMPLETE,
        plan_sha256=_replay_plan(request, selected),
        evidence_sha256="f" * 64,
        coverage_sha256=item.candidate_set.coverage_sha256,
        coverage_grade=ArchiveSearchCoverageGrade.COMPLETE,
        candidate_count=1,
        used_citation_labels=("A1.1",),
        selected_evidence=selected,
        publication_attested=True,
        semantic_verified=True,
        answer_sha256=_sha(answer),
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


def _publish_replay_failure(
    storage: Any,
    item: Any,
    *,
    ordinal: int,
    status: ArchiveRecallStatus = ArchiveRecallStatus.DENIED,
) -> tuple[Any, Any, Any, str]:
    request = "Второй" if ordinal == 2 else "Первый"
    boundary = storage.store_message(
        item.conversation_id,
        item.user_id,
        "user",
        request,
        reply_to=item.question.prompt_assistant_message_id,
    )
    selected = _archive_selection(item.candidate_set, ordinal)
    outcome = ArchiveRecallOutcome(
        lane=ArchiveRecallLane.SELECTED_EVIDENCE_REPLAY,
        status=status,
        plan_sha256=_replay_plan(request, selected),
        evidence_sha256=_sha(f"source-free:{status.value}"),
        coverage_sha256=item.candidate_set.coverage_sha256,
        coverage_grade=ArchiveSearchCoverageGrade.COMPLETE,
        candidate_count=0,
        used_citation_labels=(),
        selected_evidence=None,
        publication_attested=True,
        semantic_verified=False,
        answer_sha256=_sha(ARCHIVE_EVIDENCE_REPLAY_UNAVAILABLE),
    )
    metadata, outcome_sha256 = _metadata(outcome)
    assistant = storage.store_message(
        item.conversation_id,
        item.user_id,
        "assistant",
        ARCHIVE_EVIDENCE_REPLAY_UNAVAILABLE,
        metadata=metadata,
        reply_to=boundary["id"],
    )
    return boundary, assistant, outcome, outcome_sha256


def test_candidate_set_contract_is_closed_ordered_and_body_free() -> None:
    first = _candidate(owner="contract-owner", ordinal=1)
    second = _candidate(owner="contract-owner", ordinal=2)
    candidate_set = ArchiveCandidateSet(
        id="cset_0123456789abcdef",
        work_item_id="work_0123456789abcdef",
        evidence_sha256="e" * 64,
        coverage_sha256="c" * 64,
        coverage_grade=SelectedArchiveCoverageGrade.PARTIAL,
        authority_projection_sha256="a" * 64,
        origin_boundary_user_message_id="msg_0123456789abcdef",
        candidates=(first, second),
    )

    encoded = candidate_set.to_json()
    assert candidate_set == ArchiveCandidateSet.from_storage_rows(
        candidate_set.set_storage_payload(),
        candidate_set.item_storage_payloads(),
    )
    assert not any(
        prohibited in encoded.casefold()
        for prohibited in ("query", "title", "filename", "excerpt", "prompt", "model_prose")
    )
    with pytest.raises(ArchiveCandidateSelectionError, match="ordering/cardinality"):
        replace(candidate_set, candidates=(second, first))
    with pytest.raises(ArchiveCandidateSelectionError, match="unique"):
        replace(candidate_set, candidates=(first, replace(first, ordinal=2)))
    with pytest.raises(ArchiveCandidateSelectionError, match="outside"):
        candidate_set.selected_evidence(3)


def test_store_rejects_candidate_order_not_sealed_by_accepted_outcome(storage: Any) -> None:
    owner = "candidate-authority-owner"
    storage.ensure_user(owner, source="local")
    conversation = storage.create_conversation(owner, "private candidate authority")
    boundary = storage.store_message(conversation["id"], owner, "user", "private query")
    accepted = _projection(owner)
    answer = "Сравнение [A1.1] и [A2.1].\n\n" + archive_candidate_selection_offer_suffix(("A1", "A2"))
    outcome = _initial_outcome(answer=answer, projection=accepted)
    metadata, outcome_sha256 = _metadata(outcome)
    assistant = storage.store_message(
        conversation["id"],
        owner,
        "assistant",
        answer,
        metadata=metadata,
        reply_to=boundary["id"],
    )

    with (
        storage.transaction() as conn,
        pytest.raises(
            WorkItemAnchorError,
            match="closed set",
        ),
    ):
        create_archive_candidate_selection_work_item_in_transaction(
            conn,
            user_id=owner,
            conversation_id=conversation["id"],
            accepted_candidate_projection=_projection(owner, reverse_sources=True),
            anchor_user_message_id=boundary["id"],
            anchor_assistant_message_id=assistant["id"],
            accepted_plan_sha256=outcome.plan_sha256,
            accepted_outcome_sha256=outcome_sha256,
            now=_NOW,
        )
    assert storage.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0


def test_store_binds_exact_plain_label_offer_to_citation_order_and_subset(storage: Any) -> None:
    owner = "candidate-label-offer-owner"
    storage.ensure_user(owner, source="local")
    conversation = storage.create_conversation(owner, "private label mapping")
    boundary = storage.store_message(conversation["id"], owner, "user", "private query")
    projection = _projection(owner, public_citation_labels=("A2", "A1"))
    answer = "Сначала [A2.1], затем [A1.1].\n\n" + archive_candidate_selection_offer_suffix(("A2", "A1"))
    outcome = replace(
        _initial_outcome(answer=answer, projection=projection),
        candidate_count=5,
        used_citation_labels=("A2.1", "A1.1"),
    )
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
        created = create_archive_candidate_selection_work_item_in_transaction(
            conn,
            user_id=owner,
            conversation_id=conversation["id"],
            accepted_candidate_projection=projection,
            anchor_user_message_id=boundary["id"],
            anchor_assistant_message_id=assistant["id"],
            accepted_plan_sha256=outcome.plan_sha256,
            accepted_outcome_sha256=outcome_sha256,
            now=_NOW,
        )
    assert tuple(candidate.public_citation_label for candidate in created.candidate_set.candidates) == (
        "A2",
        "A1",
    )
    assert "1 — A2\n2 — A1" in answer
    assert "[A2]" not in answer.partition("\n\n")[2]

    invalid_owner = "candidate-invalid-label-offer-owner"
    storage.ensure_user(invalid_owner, source="local")
    invalid_conversation = storage.create_conversation(invalid_owner, "private wrong mapping")
    invalid_boundary = storage.store_message(
        invalid_conversation["id"], invalid_owner, "user", "private query"
    )
    invalid_projection = _projection(
        invalid_owner,
        public_citation_labels=("A2", "A1"),
    )
    invalid_answer = "Сначала [A2.1], затем [A1.1].\n\n" + (
        archive_candidate_selection_offer_suffix(("A1", "A2"))
    )
    invalid_outcome = _initial_outcome(answer=invalid_answer, projection=invalid_projection)
    invalid_metadata, invalid_outcome_sha256 = _metadata(invalid_outcome)
    invalid_assistant = storage.store_message(
        invalid_conversation["id"],
        invalid_owner,
        "assistant",
        invalid_answer,
        metadata=invalid_metadata,
        reply_to=invalid_boundary["id"],
    )
    with storage.transaction() as conn, pytest.raises(WorkItemAnchorError, match="closed set"):
        create_archive_candidate_selection_work_item_in_transaction(
            conn,
            user_id=invalid_owner,
            conversation_id=invalid_conversation["id"],
            accepted_candidate_projection=invalid_projection,
            anchor_user_message_id=invalid_boundary["id"],
            anchor_assistant_message_id=invalid_assistant["id"],
            accepted_plan_sha256=invalid_outcome.plan_sha256,
            accepted_outcome_sha256=invalid_outcome_sha256,
            now=_NOW,
        )


@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        ("второй", 2),
        ("  ВТОРОЙ! ", 2),
        ("2-й", 2),
        ("second", 2),
        ("20th", 20),
        ("20", 20),
        ("21", None),
        ("second source", None),
        ("выбери второй", None),
        ("2 и 3", None),
        ("2nd please", None),
    ],
)
def test_ordinal_parser_is_closed_standalone_and_set_independent(
    surface: str,
    expected: int | None,
) -> None:
    assert parse_archive_candidate_ordinal(surface) == expected


def test_source_free_reask_moves_prompt_cas_and_next_valid_ordinal_completes(storage: Any) -> None:
    item = _create_work(storage, "candidate-reask-owner")
    invalid = storage.store_message(
        item.conversation_id,
        item.user_id,
        "user",
        "двадцатый",
        reply_to=item.question.prompt_assistant_message_id,
    )
    prompt = archive_candidate_reask_prompt(item.question.maximum_ordinal)
    assistant = storage.store_message(
        item.conversation_id,
        item.user_id,
        "assistant",
        prompt,
        metadata={
            "structural": {
                "answer_present": True,
                "model_spoke": False,
                "verdict_kind": ARCHIVE_CANDIDATE_REASK_VERDICT_KIND,
            }
        },
        reply_to=invalid["id"],
    )
    with storage.transaction() as conn:
        reasked = reask_archive_candidate_selection_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=item.revision,
            invalid_boundary_user_message_id=invalid["id"],
            new_question_assistant_message_id=assistant["id"],
            now="2026-08-25T05:02:00+00:00",
        )
        current = get_current_archive_candidate_selection_work_item_in_transaction(
            conn,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            now="2026-08-25T05:02:00+00:00",
        )
    assert current == reasked
    assert reasked.revision == reasked.question.prompt_revision == 2
    assert reasked.transition is WorkTransition.QUESTION_REASKED
    assert reasked.question.prompt_boundary_user_message_id == invalid["id"]
    assert reasked.question.prompt_assistant_message_id == assistant["id"]
    assert reasked.anchor_user_message_id == item.anchor_user_message_id
    assert reasked.anchor_assistant_message_id == item.anchor_assistant_message_id
    assert reasked.accepted_outcome_sha256 == item.accepted_outcome_sha256
    assert reasked.expires_at > item.expires_at

    boundary, replay, outcome, outcome_sha256 = _publish_replay(storage, reasked, ordinal=2)
    with storage.transaction() as conn:
        completed = accept_archive_candidate_selection_in_transaction(
            conn,
            work_item_id=reasked.id,
            user_id=reasked.user_id,
            conversation_id=reasked.conversation_id,
            expected_revision=reasked.revision,
            selected_ordinal=2,
            new_boundary_user_message_id=boundary["id"],
            new_assistant_message_id=replay["id"],
            new_accepted_plan_sha256=outcome.plan_sha256,
            new_accepted_outcome_sha256=outcome_sha256,
            now="2026-08-25T05:03:00+00:00",
        )
    assert completed.state is WorkState.COMPLETED
    assert completed.revision == 3
    assert completed.question.selected_ordinal == 2


def test_reask_wrong_body_race_expiry_and_foreign_owner_are_non_mutating(storage: Any) -> None:
    item = _create_work(storage, "candidate-reask-negative-owner")
    invalid = storage.store_message(
        item.conversation_id,
        item.user_id,
        "user",
        "не уверен",
        reply_to=item.question.prompt_assistant_message_id,
    )
    wrong = storage.store_message(
        item.conversation_id,
        item.user_id,
        "assistant",
        archive_candidate_reask_prompt(item.question.maximum_ordinal) + " ",
        metadata={
            "structural": {
                "answer_present": True,
                "model_spoke": False,
                "verdict_kind": ARCHIVE_CANDIDATE_REASK_VERDICT_KIND,
            }
        },
        reply_to=invalid["id"],
    )
    with storage.transaction() as conn, pytest.raises(WorkItemAnchorError, match="source-free"):
        reask_archive_candidate_selection_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=item.revision,
            invalid_boundary_user_message_id=invalid["id"],
            new_question_assistant_message_id=wrong["id"],
            now="2026-08-25T05:01:00+00:00",
        )
    assert tuple(
        storage.execute(
            """SELECT state,prompt_revision,prompt_assistant_message_id
                 FROM work_item_archive_candidate_questions WHERE work_item_id=?""",
            (item.id,),
        ).fetchone()
    ) == ("waiting", 1, item.question.prompt_assistant_message_id)

    failures: list[str] = []
    for owner, revision, now in (
        ("foreign-owner", item.revision, _NOW),
        (item.user_id, item.revision + 1, _NOW),
        (item.user_id, item.revision, _AFTER_EXPIRY),
    ):
        with storage.transaction() as conn, pytest.raises(WorkItemConflictError) as raised:
            reask_archive_candidate_selection_in_transaction(
                conn,
                work_item_id=item.id,
                user_id=owner,
                conversation_id=item.conversation_id,
                expected_revision=revision,
                invalid_boundary_user_message_id=invalid["id"],
                new_question_assistant_message_id=wrong["id"],
                now=now,
            )
        failures.append(str(raised.value))
    assert len(set(failures)) == 1
    with storage.transaction() as conn:
        assert (
            get_current_archive_candidate_selection_work_item_in_transaction(
                conn,
                user_id="foreign-owner",
                conversation_id=item.conversation_id,
                now=_NOW,
            )
            is None
        )


def test_waiting_candidate_survives_restart_and_persists_no_content_carrier(
    settings: Any,
    tmp_path: Path,
) -> None:
    database = tmp_path / "candidate-restart.sqlite3"
    first = FridayStorage(replace(settings, database_path=database))
    try:
        item = _create_work(first, "candidate-restart-owner")
        rows = []
        for table in (
            "work_items",
            "work_item_archive_candidate_sets",
            "work_item_archive_candidate_set_items",
            "work_item_archive_candidate_questions",
        ):
            rows.extend(dict(row) for row in first.execute(f'SELECT * FROM "{table}"'))  # nosec B608
        persisted = json.dumps(rows, ensure_ascii=False, sort_keys=True)
        assert all(canary not in persisted for canary in (_QUERY_CANARY, _TITLE_CANARY, _EXCERPT_CANARY))
        validate_work_item_schema(first.conn)
    finally:
        first.close()

    reopened = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        with reopened.transaction() as conn:
            loaded = get_archive_candidate_selection_work_item_in_transaction(
                conn,
                work_item_id=item.id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
            )
            current = get_current_archive_candidate_selection_work_item_in_transaction(
                conn,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                now=_NOW,
            )
        assert loaded == current == item
    finally:
        reopened.close()


def test_exact_ordinal_replay_completes_once_and_invalid_ordinal_is_non_mutating(storage: Any) -> None:
    item = _create_work(storage, "candidate-cas-owner")
    with storage.transaction() as conn, pytest.raises(WorkItemConflictError, match="ordinal"):
        accept_archive_candidate_selection_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=item.revision,
            selected_ordinal=3,
            new_boundary_user_message_id="msg_0000000000000001",
            new_assistant_message_id="msg_0000000000000002",
            new_accepted_plan_sha256="a" * 64,
            new_accepted_outcome_sha256="b" * 64,
            now="2026-08-25T05:01:00+00:00",
        )
    question = storage.execute(
        "SELECT state,selected_ordinal,answered_at FROM work_item_archive_candidate_questions"
    ).fetchone()
    assert tuple(question) == ("waiting", None, None)

    boundary, assistant, outcome, outcome_sha256 = _publish_replay(storage, item, ordinal=2)
    with storage.transaction() as conn:
        completed = accept_archive_candidate_selection_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=item.revision,
            selected_ordinal=2,
            new_boundary_user_message_id=boundary["id"],
            new_assistant_message_id=assistant["id"],
            new_accepted_plan_sha256=outcome.plan_sha256,
            new_accepted_outcome_sha256=outcome_sha256,
            now="2026-08-25T05:01:00+00:00",
        )
    assert completed.state is WorkState.COMPLETED
    assert completed.transition is WorkTransition.CANDIDATE_REPLAYED
    assert completed.question.selected_ordinal == 2
    assert completed.anchor_user_message_id == item.anchor_user_message_id
    assert completed.anchor_assistant_message_id == item.anchor_assistant_message_id
    assert completed.accepted_plan_sha256 == item.accepted_plan_sha256
    assert completed.accepted_outcome_sha256 == item.accepted_outcome_sha256
    assert completed.question.replay_boundary_user_message_id == boundary["id"]
    assert completed.question.replay_assistant_message_id == assistant["id"]
    assert completed.question.accepted_replay_plan_sha256 == outcome.plan_sha256
    assert completed.question.accepted_replay_outcome_sha256 == outcome_sha256
    assert completed.selected_evidence == item.candidate_set.selected_evidence(2)
    with storage.transaction() as conn, pytest.raises(WorkItemConflictError, match="state"):
        accept_archive_candidate_selection_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=item.revision,
            selected_ordinal=2,
            new_boundary_user_message_id=boundary["id"],
            new_assistant_message_id=assistant["id"],
            new_accepted_plan_sha256=outcome.plan_sha256,
            new_accepted_outcome_sha256=outcome_sha256,
            now="2026-08-25T05:01:00+00:00",
        )


def test_accept_rolls_back_question_cas_when_work_completion_fails(storage: Any) -> None:
    item = _create_work(storage, "candidate-savepoint-owner")
    boundary, assistant, outcome, outcome_sha256 = _publish_replay(storage, item, ordinal=1)

    with storage.transaction() as conn:
        conn.execute(
            """CREATE TRIGGER test_block_candidate_completion
               BEFORE UPDATE OF state ON work_items
               WHEN OLD.kind='select_archive_candidate_and_replay_evidence'
                AND NEW.state='completed'
               BEGIN
                   SELECT RAISE(ABORT, 'blocked candidate completion');
               END"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="blocked candidate completion"):
            accept_archive_candidate_selection_in_transaction(
                conn,
                work_item_id=item.id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                expected_revision=item.revision,
                selected_ordinal=1,
                new_boundary_user_message_id=boundary["id"],
                new_assistant_message_id=assistant["id"],
                new_accepted_plan_sha256=outcome.plan_sha256,
                new_accepted_outcome_sha256=outcome_sha256,
                now="2026-08-25T05:01:00+00:00",
            )
        assert tuple(
            conn.execute(
                """SELECT state,selected_ordinal,answered_at
                     FROM work_item_archive_candidate_questions
                    WHERE work_item_id=?""",
                (item.id,),
            ).fetchone()
        ) == ("waiting", None, None)
        assert tuple(
            conn.execute(
                "SELECT state,revision,transition FROM work_items WHERE id=?",
                (item.id,),
            ).fetchone()
        ) == ("waiting_for_input", 1, "question_asked")
        conn.execute("DROP TRIGGER test_block_candidate_completion")

    with storage.transaction() as conn:
        completed = accept_archive_candidate_selection_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=item.revision,
            selected_ordinal=1,
            new_boundary_user_message_id=boundary["id"],
            new_assistant_message_id=assistant["id"],
            new_accepted_plan_sha256=outcome.plan_sha256,
            new_accepted_outcome_sha256=outcome_sha256,
            now="2026-08-25T05:01:00+00:00",
        )
    assert completed.state is WorkState.COMPLETED
    assert completed.question.selected_ordinal == 1


@pytest.mark.parametrize(
    "status",
    [
        ArchiveRecallStatus.DENIED,
        ArchiveRecallStatus.DRIFTED,
        ArchiveRecallStatus.UNAVAILABLE,
    ],
)
def test_source_free_replay_failure_receipt_suspends_unanswered_candidate(
    storage: Any,
    status: ArchiveRecallStatus,
) -> None:
    item = _create_work(storage, f"candidate-failure-{status.value}")
    boundary, assistant, outcome, outcome_sha256 = _publish_replay_failure(
        storage,
        item,
        ordinal=2,
        status=status,
    )

    with storage.transaction() as conn:
        suspended = suspend_after_replay_failure_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=item.revision,
            selected_ordinal=2,
            new_boundary_user_message_id=boundary["id"],
            new_assistant_message_id=assistant["id"],
            new_accepted_plan_sha256=outcome.plan_sha256,
            new_accepted_outcome_sha256=outcome_sha256,
            now="2026-08-25T05:01:00+00:00",
        )
        loaded = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
        )

    assert loaded == suspended
    assert suspended.state is WorkState.SUSPENDED
    assert suspended.transition is WorkTransition.SUSPENDED
    assert suspended.revision == item.revision + 1
    assert suspended.question.state.value == "waiting"
    assert suspended.question.selected_ordinal is None
    assert suspended.selected_evidence is None
    assert suspended.question.failed_ordinal == 2
    assert suspended.failed_evidence == item.candidate_set.selected_evidence(2)
    assert suspended.question.failure_boundary_user_message_id == boundary["id"]
    assert suspended.question.failure_assistant_message_id == assistant["id"]
    assert suspended.question.accepted_failure_plan_sha256 == outcome.plan_sha256
    assert suspended.question.accepted_failure_outcome_sha256 == outcome_sha256
    assert suspended.anchor_user_message_id == item.anchor_user_message_id
    assert suspended.anchor_assistant_message_id == item.anchor_assistant_message_id
    export = storage.export_user(item.user_id)
    exported = json.dumps(
        json.loads(Path(export["path"]).read_text(encoding="utf-8"))["work_items"],
        ensure_ascii=False,
    )
    assert ARCHIVE_EVIDENCE_REPLAY_UNAVAILABLE not in exported


def test_replay_failure_candidate_binding_and_savepoint_are_fail_closed(storage: Any) -> None:
    item = _create_work(storage, "candidate-failure-cas-owner")
    boundary, assistant, outcome, outcome_sha256 = _publish_replay_failure(
        storage,
        item,
        ordinal=1,
    )
    with storage.transaction() as conn, pytest.raises(WorkItemAnchorError, match="plan"):
        suspend_after_replay_failure_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=item.revision,
            selected_ordinal=2,
            new_boundary_user_message_id=boundary["id"],
            new_assistant_message_id=assistant["id"],
            new_accepted_plan_sha256=outcome.plan_sha256,
            new_accepted_outcome_sha256=outcome_sha256,
            now="2026-08-25T05:01:00+00:00",
        )
    assert tuple(
        storage.execute(
            """SELECT state,selected_ordinal,failed_ordinal,failure_recorded_at
                 FROM work_item_archive_candidate_questions WHERE work_item_id=?""",
            (item.id,),
        ).fetchone()
    ) == ("waiting", None, None, None)

    rollback_item = _create_work(storage, "candidate-failure-rollback-owner")
    boundary, assistant, outcome, outcome_sha256 = _publish_replay_failure(
        storage,
        rollback_item,
        ordinal=1,
    )
    with storage.transaction() as conn:
        conn.execute(
            """CREATE TRIGGER test_block_candidate_failure_suspend
               BEFORE UPDATE OF state ON work_items
               WHEN OLD.kind='select_archive_candidate_and_replay_evidence'
                AND NEW.state='suspended'
               BEGIN
                   SELECT RAISE(ABORT, 'blocked candidate failure suspend');
               END"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="blocked candidate failure suspend"):
            suspend_after_replay_failure_in_transaction(
                conn,
                work_item_id=rollback_item.id,
                user_id=rollback_item.user_id,
                conversation_id=rollback_item.conversation_id,
                expected_revision=rollback_item.revision,
                selected_ordinal=1,
                new_boundary_user_message_id=boundary["id"],
                new_assistant_message_id=assistant["id"],
                new_accepted_plan_sha256=outcome.plan_sha256,
                new_accepted_outcome_sha256=outcome_sha256,
                now="2026-08-25T05:01:00+00:00",
            )
        assert tuple(
            conn.execute(
                """SELECT state,selected_ordinal,failed_ordinal,failure_recorded_at
                     FROM work_item_archive_candidate_questions WHERE work_item_id=?""",
                (rollback_item.id,),
            ).fetchone()
        ) == ("waiting", None, None, None)
        assert tuple(
            conn.execute(
                "SELECT state,revision,transition FROM work_items WHERE id=?",
                (rollback_item.id,),
            ).fetchone()
        ) == ("waiting_for_input", 1, "question_asked")


def test_candidate_lifecycle_conversation_export_account_delete_and_backup(storage: Any) -> None:
    from friday.diagnostics.runtime_lease import ProcessLease

    owner = "local:candidate-privacy-owner"
    item = _create_work(storage, owner)

    exported = storage.export_user(owner)
    payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
    assert [work["id"] for work in payload["work_items"]] == [item.id]
    assert payload["work_items"][0]["question"]["state"] == "waiting"
    assert _QUERY_CANARY not in json.dumps(payload["work_items"], ensure_ascii=False)

    backup = storage.create_backup(label="candidate-foundation")
    with sqlite3.connect(storage.settings.backups_dir / backup["database"]) as copy:
        assert (
            copy.execute(
                "SELECT COUNT(*) FROM work_item_archive_candidate_set_items WHERE work_item_id=?",
                (item.id,),
            ).fetchone()[0]
            == 2
        )
        validate_work_item_schema(copy)

    storage.delete_conversation(item.conversation_id, owner)
    with ProcessLease(storage.settings.state_dir / "backend.lock", protocol="friday.backend.v1"):
        restored = storage.restore_backup(
            backup["database"],
            safety_label="candidate-foundation-pre-restore",
        )
    assert restored["ok"] is True
    with storage.transaction() as conn:
        restored_item = get_archive_candidate_selection_work_item_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=owner,
            conversation_id=item.conversation_id,
        )
    assert restored_item == item

    report = storage.delete_conversation(item.conversation_id, owner)
    assert report["cancelled"] == {"work_items": 1}
    cancelled = storage.execute(
        "SELECT state,transition FROM work_items WHERE id=?",
        (item.id,),
    ).fetchone()
    assert tuple(cancelled) == ("cancelled", "cancelled")

    assert _mark_account_deletion_history_clean(storage, owner)
    storage.update_user(owner, status="disabled")
    plan = preflight_account_deletion(storage, owner, quiescence_available=True)
    assert plan["counts"]["work_item_archive_candidate_sets"] == 1
    assert plan["counts"]["work_item_archive_candidate_set_items"] == 2
    assert plan["counts"]["work_item_archive_candidate_questions"] == 1
    assert plan["unknown_scopes"] == []


def test_expired_candidate_rejects_selection_and_exact_schema39_upgrades(storage: Any) -> None:
    item = _create_work(storage, "candidate-expiry-owner")
    with storage.transaction() as conn:
        expired = expire_archive_candidate_selection_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=item.revision,
            now=_AFTER_EXPIRY,
        )
    assert expired.state is WorkState.EXPIRED
    assert expired.question.selected_ordinal is None

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE TABLE users(id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE conversations(id TEXT PRIMARY KEY,user_id TEXT)")
        conn.execute(
            """CREATE TABLE messages(
                   id TEXT PRIMARY KEY,user_id TEXT,conversation_id TEXT,role TEXT)"""
        )
        _execute_schema(conn, _WORK_ITEM_SCHEMA_39)
        with pytest.raises(RuntimeError, match="existing transaction"):
            upgrade_work_item_schema_to_40(conn, required=True)
        conn.execute("BEGIN")
        upgrade_work_item_schema_to_40(conn, required=True)
        conn.commit()
        validate_work_item_schema(conn)
        assert (
            conn.execute(
                """SELECT COUNT(*) FROM sqlite_master
                WHERE type='table' AND name LIKE 'work_item_archive_candidate_%'"""
            ).fetchone()[0]
            == 3
        )
    finally:
        conn.close()


def test_exact_schema39_upgrade_preserves_selected_evidence_sidecar() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE TABLE users(id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE conversations(id TEXT PRIMARY KEY,user_id TEXT)")
        conn.execute(
            """CREATE TABLE messages(
                   id TEXT PRIMARY KEY,user_id TEXT,conversation_id TEXT,role TEXT)"""
        )
        owner = "schema39-owner"
        conversation = "conv_0123456789abcdef"
        boundary = "msg_0123456789abcdef"
        assistant = "msg_fedcba9876543210"
        work_item_id = "work_0123456789abcdef"
        conn.execute("INSERT INTO users VALUES(?)", (owner,))
        conn.execute("INSERT INTO conversations VALUES(?,?)", (conversation, owner))
        conn.execute("INSERT INTO messages VALUES(?,?,?,'user')", (boundary, owner, conversation))
        conn.execute(
            "INSERT INTO messages VALUES(?,?,?,'assistant')",
            (assistant, owner, conversation),
        )
        _execute_schema(conn, _WORK_ITEM_SCHEMA_39)
        conn.execute(
            """INSERT INTO work_items VALUES(
                   ?,?,?,'recall_selected_archive_evidence',
                   'exact_selected_archive_evidence_recall','active',
                   'recall_selected_archive_evidence','accepted_exact_selected_archive_evidence',
                   ?,?,?,'a'||substr(printf('%064d',0),2),
                   'b'||substr(printf('%064d',0),2),1,'created',?,?,?,NULL)""",
            (
                work_item_id,
                owner,
                conversation,
                RECALL_SELECTED_ARCHIVE_EVIDENCE_ACTIVE_FRAME_JSON,
                boundary,
                assistant,
                _NOW,
                _NOW,
                "2026-08-25T17:00:00+00:00",
            ),
        )
        candidate_set = ArchiveCandidateSet(
            id="cset_0123456789abcdef",
            work_item_id=work_item_id,
            evidence_sha256="e" * 64,
            coverage_sha256="c" * 64,
            coverage_grade=SelectedArchiveCoverageGrade.COMPLETE,
            authority_projection_sha256="a" * 64,
            origin_boundary_user_message_id=boundary,
            candidates=(_candidate(owner=owner, ordinal=1), _candidate(owner=owner, ordinal=2)),
        )
        evidence = candidate_set.selected_evidence(1)
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
        expected = tuple(conn.execute("SELECT * FROM work_item_selected_evidence").fetchone())
        upgrade_work_item_schema_to_40(conn, required=True)
        conn.commit()

        assert tuple(conn.execute("SELECT * FROM work_item_selected_evidence").fetchone()) == expected
        validate_work_item_schema(conn)
    finally:
        conn.close()


def test_schema_validator_rejects_digest_preserving_ddl_but_tampered_candidate_rows(
    storage: Any,
) -> None:
    item = _create_work(storage, "candidate-tamper-owner")
    with storage.transaction() as conn:
        conn.execute("DROP TRIGGER trg_work_item_archive_candidate_sets_immutable")
        conn.execute(
            "UPDATE work_item_archive_candidate_sets SET candidate_set_sha256=? WHERE work_item_id=?",
            ("0" * 64, item.id),
        )
        _execute_schema(conn, WORK_ITEM_SCHEMA)

    with pytest.raises(sqlite3.DatabaseError, match="candidate Work Item data"):
        validate_work_item_schema(storage.conn)


def test_cancel_is_owner_scoped_and_cas_bounded(storage: Any) -> None:
    item = _create_work(storage, "candidate-cancel-owner")
    with storage.transaction() as conn:
        assert (
            get_archive_candidate_selection_work_item_in_transaction(
                conn,
                work_item_id=item.id,
                user_id="foreign-owner",
                conversation_id=item.conversation_id,
            )
            is None
        )
        cancelled = cancel_archive_candidate_selection_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=item.revision,
            now="2026-08-25T05:02:00+00:00",
        )
    assert cancelled.state is WorkState.CANCELLED
    with storage.transaction() as conn, pytest.raises(WorkItemConflictError, match="state"):
        cancel_archive_candidate_selection_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=item.revision,
            now="2026-08-25T05:03:00+00:00",
        )

    with (
        storage.transaction() as conn,
        pytest.raises(
            sqlite3.IntegrityError,
            match="lifecycle",
        ),
    ):
        conn.execute(
            """UPDATE work_items
                  SET state='waiting_for_input',transition='question_asked',
                      revision=1,updated_at=?,closed_at=NULL
                WHERE id=?""",
            (_NOW, item.id),
        )
    assert tuple(
        storage.execute(
            "SELECT state,revision,transition FROM work_items WHERE id=?",
            (item.id,),
        ).fetchone()
    ) == ("cancelled", 2, "cancelled")
