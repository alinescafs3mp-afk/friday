"""Schema-44 foundation for the dormant fixed current-file/current-web graph."""

from __future__ import annotations

import gzip
import hashlib
import inspect
import json
import shutil
import sqlite3
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from pathlib import Path

import pytest

import friday.interaction_control_plane.compare_current_file_web_work_graph as graph_contract
import friday.interaction_control_plane.compare_current_file_web_work_graph_store as graph_store
from friday.account_deletion import _mark_account_deletion_history_clean, preflight_account_deletion
from friday.interaction_control_plane.compare_current_file_web_work_graph import (
    COMPARE_CURRENT_FILE_WEB_ARCHIVED_RESPONSE,
    COMPARE_CURRENT_FILE_WEB_CANCELLED_RESPONSE,
    COMPARE_CURRENT_FILE_WEB_EXPIRED_RESPONSE,
    FILE_READ_STEP_ID,
    PRIMARY_SYNTHESIS_STEP_ID,
    WEB_READ_STEP_ID,
    CompareCurrentFileWebGraphError,
    CompareCurrentFileWebGraphOutcomeReason,
    CompareCurrentFileWebGraphOutcomeStatus,
    CompareCurrentFileWebGraphState,
    CompareCurrentFileWebStepKind,
    CompareCurrentFileWebStepState,
    CompareCurrentFileWebWorkGraph,
    attach_compare_current_file_web_publication_receipt,
    attach_compare_current_file_web_terminal_publication_receipt,
    load_compare_current_file_web_terminal_publication_receipt,
)
from friday.interaction_control_plane.compare_current_file_web_work_graph_store import (
    CompareCurrentFileWebGraphAnchorError,
    CompareCurrentFileWebGraphConflictError,
    admit_compare_current_file_web_review_recovery_in_transaction,
    cancel_compare_current_file_web_work_graph_in_transaction,
    claim_compare_current_file_web_step_in_transaction,
    close_compare_current_file_web_work_graph_terminal_in_transaction,
    complete_compare_current_file_web_work_graph_in_transaction,
    create_compare_current_file_web_work_graph_in_transaction,
    expire_due_compare_current_file_web_work_graphs_in_transaction,
    get_compare_current_file_web_work_graph_in_transaction,
    get_current_compare_current_file_web_work_graph_in_transaction,
    prepare_compare_current_file_web_restart_rebind_in_transaction,
    retire_compare_current_file_web_work_graph_for_archived_conversation_in_transaction,
    settle_compare_current_file_web_step_in_transaction,
)
from friday.interaction_control_plane.work_item_schema import validate_work_item_schema
from friday.orchestration.supervisor_contracts import (
    CapabilityEffectClass,
    CompletionCriterion,
    ReviewRecommendedAction,
    ReviewVerdict,
    SupervisorReview,
)
from friday.orchestration.supervisor_review_policy import (
    DeterministicReviewState,
    ReadRecoveryCandidate,
    SupervisorReviewContext,
    admit_supervisor_review,
)
from friday.orchestration.transient_web_comparison import (
    TRANSIENT_WEB_ADAPTER_ID,
    TRANSIENT_WEB_SECURITY_ID,
)
from friday.storage import SCHEMA_VERSION, FridayStorage
from friday.storage._conversations import store_message_in_transaction
from friday.storage.models import RawObject, new_id

_SCHEMA_FIXTURES = Path(__file__).parent / "fixtures" / "schemas"
_OWNER = "schema44-graph-owner"
_READ_TERMINAL_STATES = (
    CompareCurrentFileWebStepState.COMPLETE,
    CompareCurrentFileWebStepState.PARTIAL,
    CompareCurrentFileWebStepState.EMPTY,
    CompareCurrentFileWebStepState.UNAVAILABLE,
    CompareCurrentFileWebStepState.DENIED,
    CompareCurrentFileWebStepState.FAILED,
)
_USABLE_READ_STATES = {
    CompareCurrentFileWebStepState.COMPLETE,
    CompareCurrentFileWebStepState.PARTIAL,
}


def _sha256(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _next_instant(graph: CompareCurrentFileWebWorkGraph) -> str:
    from datetime import datetime

    return (datetime.fromisoformat(graph.updated_at) + timedelta(seconds=1)).isoformat()


def _seed_graph(
    storage: FridayStorage,
    label: str,
    *,
    owner: str = _OWNER,
    now: str = "2026-08-26T10:00:00+00:00",
    expires_at: str = "2026-08-26T22:00:00+00:00",
) -> CompareCurrentFileWebWorkGraph:
    storage.ensure_user(owner, source="schema44-test")
    conversation = storage.create_conversation(owner, f"schema44 {label}")
    boundary = storage.store_message(
        conversation["id"],
        owner,
        "user",
        f"synthetic current-file/current-web request {label}",
    )
    raw_id = new_id("raw")
    storage.store_raw_object(
        RawObject(
            id=raw_id,
            user_id=owner,
            source="upload",
            source_ref=f"sha256:{_sha256(f'source:{label}')}",
            raw_content=f"synthetic current file {label}",
            content_type="text/plain",
            content_hash=_sha256(f"content:{label}"),
        )
    )
    kinds = tuple(CompareCurrentFileWebStepKind)
    graph = CompareCurrentFileWebWorkGraph.admitted(
        user_id=owner,
        conversation_id=str(conversation["id"]),
        anchor_user_message_id=str(boundary["id"]),
        current_file_raw_object_id=raw_id,
        proposal_sha256=_sha256(f"proposal:{label}"),
        accepted_plan_sha256=_sha256(f"plan:{label}"),
        manifest_sha256=_sha256(f"manifest:{label}"),
        policy_sha256=_sha256(f"policy:{label}"),
        runtime_profile_sha256=_sha256(f"runtime:{label}"),
        adapter_registry_sha256=_sha256(f"adapters:{label}"),
        actor_binding_sha256=_sha256(f"actor:{owner}"),
        conversation_binding_sha256=_sha256(f"conversation:{conversation['id']}"),
        current_file_source_identity_sha256=_sha256(f"source-identity:{raw_id}"),
        current_file_content_sha256=_sha256(f"file-content:{label}"),
        step_input_identities={kind: _sha256(f"input:{label}:{kind.value}") for kind in kinds},
        step_idempotency_keys={kind: _sha256(f"idempotency:{label}:{kind.value}") for kind in kinds},
        now=now,
        expires_at=expires_at,
    )
    with storage.transaction() as conn:
        return create_compare_current_file_web_work_graph_in_transaction(conn, graph)


def _claim(
    storage: FridayStorage,
    graph: CompareCurrentFileWebWorkGraph,
    step_id: str,
) -> CompareCurrentFileWebWorkGraph:
    with storage.transaction() as conn:
        return claim_compare_current_file_web_step_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            step_id=step_id,
            now=_next_instant(graph),
        )


def _settle(
    storage: FridayStorage,
    graph: CompareCurrentFileWebWorkGraph,
    step_id: str,
    state: CompareCurrentFileWebStepState,
) -> CompareCurrentFileWebWorkGraph:
    step = graph.step(step_id)
    accepted = state in {
        CompareCurrentFileWebStepState.COMPLETE,
        CompareCurrentFileWebStepState.PARTIAL,
        CompareCurrentFileWebStepState.EMPTY,
    }
    is_read = step.kind in {
        CompareCurrentFileWebStepKind.FILE_READ,
        CompareCurrentFileWebStepKind.WEB_READ,
    }
    with storage.transaction() as conn:
        return settle_compare_current_file_web_step_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            step_id=step_id,
            state=state,
            outcome_sha256=_sha256(
                f"outcome:{graph.id}:{step_id}:{step.attempt}:{state.value}:{graph.revision}"
            ),
            evidence_identity_sha256=(
                _sha256(f"evidence:{graph.id}:{step_id}:{step.attempt}:{state.value}") if accepted else None
            ),
            authority_rechecked=bool(
                is_read
                and state
                in {
                    CompareCurrentFileWebStepState.COMPLETE,
                    CompareCurrentFileWebStepState.PARTIAL,
                    CompareCurrentFileWebStepState.EMPTY,
                    CompareCurrentFileWebStepState.DENIED,
                }
            ),
            verified=accepted,
            now=_next_instant(graph),
        )


def _claim_and_settle(
    storage: FridayStorage,
    graph: CompareCurrentFileWebWorkGraph,
    step_id: str,
    state: CompareCurrentFileWebStepState,
) -> CompareCurrentFileWebWorkGraph:
    return _settle(storage, _claim(storage, graph, step_id), step_id, state)


def _terminal_expected(
    left: CompareCurrentFileWebStepState,
    right: CompareCurrentFileWebStepState,
) -> tuple[CompareCurrentFileWebGraphOutcomeStatus, CompareCurrentFileWebGraphOutcomeReason]:
    states = {left, right}
    if CompareCurrentFileWebStepState.DENIED in states:
        return (
            CompareCurrentFileWebGraphOutcomeStatus.DENIED,
            CompareCurrentFileWebGraphOutcomeReason.AUTHORITY_DENIED,
        )
    if CompareCurrentFileWebStepState.FAILED in states:
        return (
            CompareCurrentFileWebGraphOutcomeStatus.FAILED,
            CompareCurrentFileWebGraphOutcomeReason.STEP_FAILED,
        )
    if CompareCurrentFileWebStepState.UNAVAILABLE in states:
        return (
            CompareCurrentFileWebGraphOutcomeStatus.UNAVAILABLE,
            CompareCurrentFileWebGraphOutcomeReason.CAPABILITY_UNAVAILABLE,
        )
    return (
        CompareCurrentFileWebGraphOutcomeStatus.EMPTY,
        CompareCurrentFileWebGraphOutcomeReason.NO_COMPARABLE_EVIDENCE,
    )


def _admitted_web_recovery(graph: CompareCurrentFileWebWorkGraph):
    step = graph.step(WEB_READ_STEP_ID)
    assert step.outcome_sha256 is not None
    criterion = CompletionCriterion.CURRENT_PUBLIC_EVIDENCE_HAS_COVERAGE
    context = SupervisorReviewContext(
        plan_digest=graph.accepted_plan_sha256,
        outcome_digest=step.outcome_sha256,
        work_item_digest=graph.canonical_sha256(),
        work_revision=graph.revision,
        deterministic_state=DeterministicReviewState.FAILED,
        failed_criteria=(criterion,),
        review_round=1,
        max_review_rounds=1,
        recovery_budget_remaining=1,
        effect_started=False,
        publication_started=False,
        recovery_candidate=ReadRecoveryCandidate(
            step_id=step.step_id,
            capability_id=step.capability_id,
            criterion=criterion,
            effect_class=CapabilityEffectClass.READ,
            idempotency_key=step.idempotency_key_sha256,
            eligible=True,
        ),
    )
    review = SupervisorReview(
        plan_digest=context.plan_digest,
        outcome_digest=context.outcome_digest,
        verdict=ReviewVerdict.RETRY_READ_ONLY_STEP,
        failed_criteria=context.failed_criteria,
        recommended_action=ReviewRecommendedAction.REQUEST_READ_ONLY_RECOVERY,
        reason_code="current_web_coverage_failed",
    )
    decision = admit_supervisor_review(review, context)
    assert decision.admitted is True
    assert decision.recovery is not None
    return decision.recovery


def test_schema44_contract_is_fixed_body_free_dormant_and_transient_web_bound(storage) -> None:
    graph = _seed_graph(storage, "fixed-contract")
    file_step, web_step, synthesis = graph.steps

    assert SCHEMA_VERSION == 44
    assert (file_step.step_id, web_step.step_id, synthesis.step_id) == (
        FILE_READ_STEP_ID,
        WEB_READ_STEP_ID,
        PRIMARY_SYNTHESIS_STEP_ID,
    )
    assert file_step.depends_on == web_step.depends_on == ()
    assert synthesis.depends_on == (FILE_READ_STEP_ID, WEB_READ_STEP_ID)
    assert file_step.parallel_group == web_step.parallel_group == "current_evidence"
    assert synthesis.parallel_group is None
    assert web_step.capability_id == "web.search.current"
    assert web_step.security_id == TRANSIENT_WEB_SECURITY_ID == "web.compare.transient"
    assert web_step.adapter_id == TRANSIENT_WEB_ADAPTER_ID == "transient_web_comparison"
    assert all(step.evidence_replayability == "process_private" for step in graph.steps)
    assert {step.payload()["effect_class"] for step in graph.steps} == {"read"}

    payload_text = repr(graph.payload())
    assert "web_research" not in payload_text
    assert "synthetic current file" not in payload_text
    assert not ({"query", "path", "body", "prompt"} & set(graph.payload()))
    contract_source = inspect.getsource(graph_contract)
    store_source = inspect.getsource(graph_store)
    assert "ExecutionKernel" not in contract_source + store_source
    assert "semantic_supervisor_runtime" not in contract_source + store_source
    assert "web_research" not in contract_source + store_source
    assert not any(name.startswith("execute_") or name.endswith("_worker") for name in graph_store.__all__)

    with pytest.raises(FrozenInstanceError):
        file_step.capability_id = "anything"  # type: ignore[misc]
    with pytest.raises(CompareCurrentFileWebGraphError, match="capability identity"):
        replace(file_step, capability_id="web.research")


def test_released_schema43_migrates_to44_without_rewriting_old_data(settings, tmp_path) -> None:
    database = tmp_path / "schema-43.sqlite3"
    with gzip.open(_SCHEMA_FIXTURES / "schema-43.sqlite3.gz", "rb") as packed, database.open("wb") as raw:
        shutil.copyfileobj(packed, raw)

    migrated = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        assert (
            migrated.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "44"
        )
        assert (
            migrated.execute("SELECT value FROM runtime_kv WHERE key='fixture:marker'").fetchone()[0]
            == "schema-43"
        )
        assert (
            migrated.execute("SELECT COUNT(*) FROM raw_objects WHERE user_id='fixture-owner'").fetchone()[0]
            == 3
        )
        assert (
            migrated.execute("SELECT COUNT(*) FROM work_item_compare_current_file_web_graphs").fetchone()[0]
            == 0
        )
        assert (
            migrated.execute("SELECT COUNT(*) FROM work_item_compare_current_file_web_steps").fetchone()[0]
            == 0
        )
        validate_work_item_schema(migrated.conn)
        assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        migrated.close()


def test_user_export_validates_and_projects_the_body_free_graph(storage) -> None:
    graph = _seed_graph(storage, "export", owner="schema44-export-owner")

    exported = storage.export_user(graph.user_id)
    payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
    graphs = payload["work_item_compare_current_file_web_graphs"]

    assert len(graphs) == 1
    assert graphs[0]["id"] == graph.id
    assert graphs[0]["accepted_plan_sha256"] == graph.accepted_plan_sha256
    assert [step["step_id"] for step in graphs[0]["steps"]] == [
        FILE_READ_STEP_ID,
        WEB_READ_STEP_ID,
        PRIMARY_SYNTHESIS_STEP_ID,
    ]
    exported_text = json.dumps(graphs, ensure_ascii=False, sort_keys=True)
    assert "synthetic current file" not in exported_text
    assert "synthetic current-file/current-web request" not in exported_text
    assert "web_research" not in exported_text


def test_account_deletion_inventory_classifies_graph_and_steps(storage) -> None:
    owner = "local:schema44-graph-delete-owner"
    _seed_graph(storage, "deletion-inventory", owner=owner)
    assert _mark_account_deletion_history_clean(storage, owner)
    storage.update_user(owner, status="disabled")

    plan = preflight_account_deletion(storage, owner, quiescence_available=True)

    assert plan["counts"]["work_item_compare_current_file_web_graphs"] == 1
    assert plan["counts"]["work_item_compare_current_file_web_steps"] == 3
    assert plan["unknown_scopes"] == []
    assert {item["code"] for item in plan["blockers"]} == {"chat_history"}


def test_graph_restart_read_and_parent_revision_cas_are_exact(settings, tmp_path) -> None:
    database = tmp_path / "restart.sqlite3"
    first = FridayStorage(replace(settings, database_path=database))
    graph = _seed_graph(first, "restart-cas")
    initial_sha256 = graph.canonical_sha256()
    with (
        pytest.raises(CompareCurrentFileWebGraphConflictError, match="revision/state"),
        first.transaction() as conn,
    ):
        claim_compare_current_file_web_step_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision + 1,
            step_id=FILE_READ_STEP_ID,
            now=_next_instant(graph),
        )
    graph = _claim(first, graph, FILE_READ_STEP_ID)
    first.close()

    reopened = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        with reopened.transaction() as conn:
            restored = get_compare_current_file_web_work_graph_in_transaction(
                conn,
                graph_id=graph.id,
                user_id=graph.user_id,
                conversation_id=graph.conversation_id,
            )
        assert restored == graph
        assert restored is not None
        assert restored.step(FILE_READ_STEP_ID).attempt == 1
        assert initial_sha256 != restored.canonical_sha256()

        # The independent web read can be claimed while the file read is still running.
        restored = _claim(reopened, restored, WEB_READ_STEP_ID)
        assert restored.step(FILE_READ_STEP_ID).state is CompareCurrentFileWebStepState.RUNNING
        assert restored.step(WEB_READ_STEP_ID).state is CompareCurrentFileWebStepState.RUNNING

        for sql in (
            """UPDATE work_item_compare_current_file_web_steps
                   SET adapter_id='web_research'
                 WHERE graph_id=? AND step_id='read_current_web'""",
            """UPDATE work_item_compare_current_file_web_graphs
                   SET current_file_content_sha256=? WHERE id=?""",
            """DELETE FROM work_item_compare_current_file_web_steps
                 WHERE graph_id=? AND step_id='read_current_web'""",
        ):
            with pytest.raises(sqlite3.IntegrityError), reopened.transaction() as conn:
                if "current_file_content" in sql:
                    conn.execute(sql, (_sha256("tampered"), restored.id))
                else:
                    conn.execute(sql, (restored.id,))
    finally:
        reopened.close()


def test_every_settled_read_pair_has_primary_or_terminal_path(storage) -> None:
    index = 0
    for file_state in _READ_TERMINAL_STATES:
        for web_state in _READ_TERMINAL_STATES:
            graph = _seed_graph(storage, f"matrix-{index}")
            index += 1
            graph = _claim_and_settle(storage, graph, FILE_READ_STEP_ID, file_state)
            graph = _claim_and_settle(storage, graph, WEB_READ_STEP_ID, web_state)
            has_usable_evidence = bool({file_state, web_state} & _USABLE_READ_STATES)
            if has_usable_evidence:
                graph = _claim(storage, graph, PRIMARY_SYNTHESIS_STEP_ID)
                if (
                    file_state is CompareCurrentFileWebStepState.COMPLETE
                    and web_state is CompareCurrentFileWebStepState.COMPLETE
                ):
                    graph = _settle(
                        storage,
                        graph,
                        PRIMARY_SYNTHESIS_STEP_ID,
                        CompareCurrentFileWebStepState.COMPLETE,
                    )
                    receipt = graph.publication_receipt(final_authority_rechecked=True)
                    assert receipt.final_authority_rechecked is True
                    with pytest.raises(CompareCurrentFileWebGraphError):
                        graph.terminal_publication_receipt(final_authority_rechecked=False)
                else:
                    with pytest.raises(CompareCurrentFileWebGraphError, match="cannot claim complete"):
                        _settle(
                            storage,
                            graph,
                            PRIMARY_SYNTHESIS_STEP_ID,
                            CompareCurrentFileWebStepState.COMPLETE,
                        )
                    graph = _settle(
                        storage,
                        graph,
                        PRIMARY_SYNTHESIS_STEP_ID,
                        CompareCurrentFileWebStepState.PARTIAL,
                    )
                    with pytest.raises(CompareCurrentFileWebGraphError, match="final source"):
                        graph.terminal_publication_receipt(final_authority_rechecked=False)
                    terminal = graph.terminal_publication_receipt(final_authority_rechecked=True)
                    assert terminal.status is CompareCurrentFileWebGraphOutcomeStatus.PARTIAL
                    assert terminal.model_spoke is terminal.evidence_cited is True
                    assert terminal.final_authority_rechecked is True
                    assert terminal.completion_claimed is False
            else:
                with pytest.raises(CompareCurrentFileWebGraphConflictError, match="not ready"):
                    _claim(storage, graph, PRIMARY_SYNTHESIS_STEP_ID)
                terminal = graph.terminal_publication_receipt(final_authority_rechecked=False)
                assert (terminal.status, terminal.reason) == _terminal_expected(file_state, web_state)
                assert terminal.model_spoke is terminal.evidence_cited is False
                assert terminal.final_authority_rechecked is terminal.completion_claimed is False


def test_primary_partial_can_seal_projection_after_two_complete_reads(storage) -> None:
    graph = _seed_graph(storage, "complete-reads-projected-primary")
    graph = _claim_and_settle(
        storage,
        graph,
        FILE_READ_STEP_ID,
        CompareCurrentFileWebStepState.COMPLETE,
    )
    graph = _claim_and_settle(
        storage,
        graph,
        WEB_READ_STEP_ID,
        CompareCurrentFileWebStepState.COMPLETE,
    )
    graph = _claim_and_settle(
        storage,
        graph,
        PRIMARY_SYNTHESIS_STEP_ID,
        CompareCurrentFileWebStepState.PARTIAL,
    )

    receipt = graph.terminal_publication_receipt(final_authority_rechecked=True)
    assert receipt.status is CompareCurrentFileWebGraphOutcomeStatus.PARTIAL
    assert receipt.reason is CompareCurrentFileWebGraphOutcomeReason.PARTIAL_EVIDENCE
    assert receipt.model_spoke is receipt.evidence_cited is True
    with storage.transaction() as conn:
        assistant = store_message_in_transaction(
            conn,
            graph.conversation_id,
            graph.user_id,
            "assistant",
            "Оба чтения завершены, но проекция сравнения частична.",
            attach_compare_current_file_web_terminal_publication_receipt({}, receipt),
            graph.anchor_user_message_id,
        )
        terminal = close_compare_current_file_web_work_graph_terminal_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            publication_assistant_message_id=str(assistant["id"]),
            receipt=receipt,
            now=_next_instant(graph),
        )
    assert terminal.state is CompareCurrentFileWebGraphState.TERMINAL
    assert terminal.publication_receipt_sha256 is None


def test_review_recovery_is_one_exact_body_free_web_cas(storage) -> None:
    for index, failed_state in enumerate(
        (
            CompareCurrentFileWebStepState.EMPTY,
            CompareCurrentFileWebStepState.UNAVAILABLE,
            CompareCurrentFileWebStepState.FAILED,
        )
    ):
        graph = _seed_graph(storage, f"review-recovery-{index}")
        graph = _claim_and_settle(storage, graph, WEB_READ_STEP_ID, failed_state)
        old_outcome = graph.step(WEB_READ_STEP_ID).outcome_sha256
        recovery = _admitted_web_recovery(graph)
        with storage.transaction() as conn:
            graph = admit_compare_current_file_web_review_recovery_in_transaction(
                conn,
                graph_id=graph.id,
                user_id=graph.user_id,
                conversation_id=graph.conversation_id,
                expected_revision=graph.revision,
                recovery=recovery,
                now=_next_instant(graph),
            )
        web = graph.step(WEB_READ_STEP_ID)
        assert graph.transition.value == "review_recovery_admitted"
        assert web.state is CompareCurrentFileWebStepState.PENDING
        assert web.attempt == 1
        assert web.outcome_sha256 is web.evidence_identity_sha256 is None
        assert web.prior_outcome_sha256 == old_outcome
        assert web.recovery_review_sha256 == recovery.review_digest
        assert web.recovery_context_sha256 == recovery.context_digest

        with (
            pytest.raises(CompareCurrentFileWebGraphConflictError, match="not eligible"),
            storage.transaction() as conn,
        ):
            admit_compare_current_file_web_review_recovery_in_transaction(
                conn,
                graph_id=graph.id,
                user_id=graph.user_id,
                conversation_id=graph.conversation_id,
                expected_revision=graph.revision,
                recovery=recovery,
                now=_next_instant(graph),
            )
        graph = _claim(storage, graph, WEB_READ_STEP_ID)
        assert graph.step(WEB_READ_STEP_ID).attempt == 2
        graph = _settle(
            storage,
            graph,
            WEB_READ_STEP_ID,
            CompareCurrentFileWebStepState.EMPTY,
        )
        with (
            pytest.raises(CompareCurrentFileWebGraphConflictError, match="not eligible"),
            storage.transaction() as conn,
        ):
            admit_compare_current_file_web_review_recovery_in_transaction(
                conn,
                graph_id=graph.id,
                user_id=graph.user_id,
                conversation_id=graph.conversation_id,
                expected_revision=graph.revision,
                recovery=recovery,
                now=_next_instant(graph),
            )

    denied = _seed_graph(storage, "review-recovery-denied")
    denied = _claim_and_settle(
        storage,
        denied,
        WEB_READ_STEP_ID,
        CompareCurrentFileWebStepState.DENIED,
    )
    with (
        pytest.raises(CompareCurrentFileWebGraphConflictError, match="not eligible"),
        storage.transaction() as conn,
    ):
        admit_compare_current_file_web_review_recovery_in_transaction(
            conn,
            graph_id=denied.id,
            user_id=denied.user_id,
            conversation_id=denied.conversation_id,
            expected_revision=denied.revision,
            recovery=_admitted_web_recovery(denied),
            now=_next_instant(denied),
        )

    guarded = _seed_graph(storage, "review-recovery-witness-guard")
    guarded = _claim_and_settle(
        storage,
        guarded,
        WEB_READ_STEP_ID,
        CompareCurrentFileWebStepState.UNAVAILABLE,
    )
    recovery = _admitted_web_recovery(guarded)
    with (
        pytest.raises(CompareCurrentFileWebGraphAnchorError, match="does not match"),
        storage.transaction() as conn,
    ):
        admit_compare_current_file_web_review_recovery_in_transaction(
            conn,
            graph_id=guarded.id,
            user_id=guarded.user_id,
            conversation_id=guarded.conversation_id,
            expected_revision=guarded.revision,
            recovery=replace(recovery, idempotency_key=_sha256("foreign-recovery-key")),
            now=_next_instant(guarded),
        )
    with pytest.raises(sqlite3.IntegrityError), storage.transaction() as conn:
        conn.execute(
            """UPDATE work_item_compare_current_file_web_steps
                  SET state='pending',outcome_sha256=NULL,
                      prior_outcome_sha256=?,recovery_review_sha256=?,
                      recovery_context_sha256=?,evidence_identity_sha256=NULL,
                      authority_rechecked=0,verified=0,started_at=NULL,settled_at=NULL
                WHERE graph_id=? AND step_id='read_current_web'""",
            (
                guarded.step(WEB_READ_STEP_ID).outcome_sha256,
                recovery.review_digest,
                recovery.context_digest,
                guarded.id,
            ),
        )


def test_restart_rebind_never_treats_digests_as_replayable_bodies(settings, tmp_path) -> None:
    database = tmp_path / "restart-rebind.sqlite3"
    first = FridayStorage(replace(settings, database_path=database))
    graph = _seed_graph(first, "restart-during-synthesis")
    graph = _claim_and_settle(first, graph, FILE_READ_STEP_ID, CompareCurrentFileWebStepState.COMPLETE)
    graph = _claim_and_settle(first, graph, WEB_READ_STEP_ID, CompareCurrentFileWebStepState.COMPLETE)
    old_inputs = {step.step_id: step.input_identity_sha256 for step in graph.steps}
    old_keys = {step.step_id: step.idempotency_key_sha256 for step in graph.steps}
    old_outcomes = {step.step_id: step.outcome_sha256 for step in graph.steps[:2]}
    graph = _claim(first, graph, PRIMARY_SYNTHESIS_STEP_ID)
    first.close()  # crash boundary: read/synthesis payloads were process-private

    reopened = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        with reopened.transaction() as conn:
            graph = prepare_compare_current_file_web_restart_rebind_in_transaction(
                conn,
                graph_id=graph.id,
                user_id=graph.user_id,
                conversation_id=graph.conversation_id,
                expected_revision=graph.revision,
                now=_next_instant(graph),
            )
        for step in graph.steps[:2]:
            assert step.state is CompareCurrentFileWebStepState.PENDING
            assert step.attempt == 1
            assert step.outcome_sha256 is step.evidence_identity_sha256 is None
            assert step.prior_outcome_sha256 == old_outcomes[step.step_id]
            assert step.input_identity_sha256 == old_inputs[step.step_id]
            assert step.idempotency_key_sha256 == old_keys[step.step_id]
        synthesis = graph.step(PRIMARY_SYNTHESIS_STEP_ID)
        assert synthesis.state is CompareCurrentFileWebStepState.PENDING
        assert synthesis.attempt == 1
        assert synthesis.outcome_sha256 is None

        for step_id in (FILE_READ_STEP_ID, WEB_READ_STEP_ID):
            graph = _claim(reopened, graph, step_id)
            assert graph.step(step_id).attempt == 2
            graph = _settle(
                reopened,
                graph,
                step_id,
                CompareCurrentFileWebStepState.COMPLETE,
            )
            assert graph.step(step_id).authority_rechecked is True
        graph = _claim(reopened, graph, PRIMARY_SYNTHESIS_STEP_ID)
        assert graph.step(PRIMARY_SYNTHESIS_STEP_ID).attempt == 2
        graph = _settle(
            reopened,
            graph,
            PRIMARY_SYNTHESIS_STEP_ID,
            CompareCurrentFileWebStepState.COMPLETE,
        )
        assert graph.publication_receipt(final_authority_rechecked=True)
    finally:
        reopened.close()


def test_exhausted_restart_closes_with_atomic_noncompletion_publication(storage) -> None:
    graph = _seed_graph(storage, "exhausted-restart")
    graph = _claim_and_settle(storage, graph, FILE_READ_STEP_ID, CompareCurrentFileWebStepState.COMPLETE)
    graph = _claim_and_settle(storage, graph, WEB_READ_STEP_ID, CompareCurrentFileWebStepState.COMPLETE)
    with storage.transaction() as conn:
        graph = prepare_compare_current_file_web_restart_rebind_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            now=_next_instant(graph),
        )
    graph = _claim_and_settle(storage, graph, FILE_READ_STEP_ID, CompareCurrentFileWebStepState.COMPLETE)
    graph = _claim_and_settle(storage, graph, WEB_READ_STEP_ID, CompareCurrentFileWebStepState.UNAVAILABLE)
    with (
        pytest.raises(CompareCurrentFileWebGraphConflictError, match="exhausted"),
        storage.transaction() as conn,
    ):
        prepare_compare_current_file_web_restart_rebind_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            now=_next_instant(graph),
        )

    receipt = graph.terminal_publication_receipt(
        evidence_not_replayable=True,
        final_authority_rechecked=False,
    )
    assert receipt.status is CompareCurrentFileWebGraphOutcomeStatus.UNAVAILABLE
    assert receipt.reason is CompareCurrentFileWebGraphOutcomeReason.EVIDENCE_NOT_REPLAYABLE
    assert receipt.model_spoke is receipt.evidence_cited is False
    assert receipt.completion_claimed is False
    metadata = attach_compare_current_file_web_terminal_publication_receipt({}, receipt)
    with storage.transaction() as conn:
        assistant = store_message_in_transaction(
            conn,
            graph.conversation_id,
            graph.user_id,
            "assistant",
            "Сравнение нельзя безопасно продолжить после перезапуска.",
            metadata,
            graph.anchor_user_message_id,
        )
        graph = close_compare_current_file_web_work_graph_terminal_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            publication_assistant_message_id=str(assistant["id"]),
            receipt=receipt,
            evidence_not_replayable=True,
            now=_next_instant(graph),
        )
    assert graph.state is CompareCurrentFileWebGraphState.TERMINAL
    assert graph.publication_receipt_sha256 is None
    assert graph.terminal_publication_receipt_sha256 == receipt.canonical_sha256()
    stored_receipt = load_compare_current_file_web_terminal_publication_receipt(
        storage.execute(
            "SELECT metadata_json FROM messages WHERE id=?",
            (graph.publication_assistant_message_id,),
        ).fetchone()[0]
    )
    assert stored_receipt == receipt


def test_terminal_publication_is_atomic_exactly_once_and_partial_rechecked(storage) -> None:
    graph = _seed_graph(storage, "negative-terminal")
    graph = _claim_and_settle(storage, graph, FILE_READ_STEP_ID, CompareCurrentFileWebStepState.UNAVAILABLE)
    graph = _claim_and_settle(storage, graph, WEB_READ_STEP_ID, CompareCurrentFileWebStepState.DENIED)
    receipt = graph.terminal_publication_receipt(final_authority_rechecked=False)
    tampered = replace(receipt, steps_sha256=_sha256("tampered-terminal-steps"))
    with (
        pytest.raises(CompareCurrentFileWebGraphAnchorError, match="does not match"),
        storage.transaction() as conn,
    ):
        assistant = store_message_in_transaction(
            conn,
            graph.conversation_id,
            graph.user_id,
            "assistant",
            "tampered fallback",
            attach_compare_current_file_web_terminal_publication_receipt({}, tampered),
            graph.anchor_user_message_id,
        )
        close_compare_current_file_web_work_graph_terminal_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            publication_assistant_message_id=str(assistant["id"]),
            receipt=tampered,
            now=_next_instant(graph),
        )
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='assistant'",
            (graph.conversation_id,),
        ).fetchone()[0]
        == 0
    )

    with storage.transaction() as conn:
        assistant = store_message_in_transaction(
            conn,
            graph.conversation_id,
            graph.user_id,
            "assistant",
            "Нет доступа к одному из источников; сравнение не выполнено.",
            attach_compare_current_file_web_terminal_publication_receipt({}, receipt),
            graph.anchor_user_message_id,
        )
        terminal = close_compare_current_file_web_work_graph_terminal_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            publication_assistant_message_id=str(assistant["id"]),
            receipt=receipt,
            now=_next_instant(graph),
        )
    assert terminal.outcome_status is CompareCurrentFileWebGraphOutcomeStatus.DENIED
    assert terminal.publication_assistant_message_id == assistant["id"]
    with (
        pytest.raises(CompareCurrentFileWebGraphConflictError, match="revision/state"),
        storage.transaction() as conn,
    ):
        close_compare_current_file_web_work_graph_terminal_in_transaction(
            conn,
            graph_id=terminal.id,
            user_id=terminal.user_id,
            conversation_id=terminal.conversation_id,
            expected_revision=terminal.revision,
            publication_assistant_message_id=str(assistant["id"]),
            receipt=receipt,
            now=_next_instant(terminal),
        )

    partial = _seed_graph(storage, "partial-terminal")
    partial = _claim_and_settle(storage, partial, FILE_READ_STEP_ID, CompareCurrentFileWebStepState.COMPLETE)
    partial = _claim_and_settle(
        storage, partial, WEB_READ_STEP_ID, CompareCurrentFileWebStepState.UNAVAILABLE
    )
    partial = _claim_and_settle(
        storage,
        partial,
        PRIMARY_SYNTHESIS_STEP_ID,
        CompareCurrentFileWebStepState.PARTIAL,
    )
    with pytest.raises(CompareCurrentFileWebGraphError, match="final source"):
        partial.terminal_publication_receipt(final_authority_rechecked=False)
    partial_receipt = partial.terminal_publication_receipt(final_authority_rechecked=True)
    assert partial_receipt.model_spoke is True
    assert partial_receipt.evidence_cited is True
    assert partial_receipt.final_authority_rechecked is True
    with storage.transaction() as conn:
        partial_assistant = store_message_in_transaction(
            conn,
            partial.conversation_id,
            partial.user_id,
            "assistant",
            "Доступна только часть сравнения.",
            attach_compare_current_file_web_terminal_publication_receipt({}, partial_receipt),
            partial.anchor_user_message_id,
        )
        partial = close_compare_current_file_web_work_graph_terminal_in_transaction(
            conn,
            graph_id=partial.id,
            user_id=partial.user_id,
            conversation_id=partial.conversation_id,
            expected_revision=partial.revision,
            publication_assistant_message_id=str(partial_assistant["id"]),
            receipt=partial_receipt,
            now=_next_instant(partial),
        )
    assert partial.outcome_status is CompareCurrentFileWebGraphOutcomeStatus.PARTIAL
    assert partial.publication_receipt_sha256 is None


def test_full_completion_receipt_is_separate_atomic_and_rechecked(storage) -> None:
    graph = _seed_graph(storage, "full-completion")
    for step_id in (FILE_READ_STEP_ID, WEB_READ_STEP_ID, PRIMARY_SYNTHESIS_STEP_ID):
        graph = _claim_and_settle(
            storage,
            graph,
            step_id,
            CompareCurrentFileWebStepState.COMPLETE,
        )
    with pytest.raises(CompareCurrentFileWebGraphError, match="final source"):
        graph.publication_receipt(final_authority_rechecked=False)
    receipt = graph.publication_receipt(final_authority_rechecked=True)
    metadata = attach_compare_current_file_web_publication_receipt({}, receipt)
    with storage.transaction() as conn:
        assistant = store_message_in_transaction(
            conn,
            graph.conversation_id,
            graph.user_id,
            "assistant",
            "Полное сравнение выполнено.",
            metadata,
            graph.anchor_user_message_id,
        )
        completed = complete_compare_current_file_web_work_graph_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            publication_assistant_message_id=str(assistant["id"]),
            receipt=receipt,
            now=_next_instant(graph),
        )
    assert completed.state is CompareCurrentFileWebGraphState.COMPLETED
    assert completed.outcome_status is CompareCurrentFileWebGraphOutcomeStatus.COMPLETE
    assert completed.outcome_reason is CompareCurrentFileWebGraphOutcomeReason.NONE
    assert completed.terminal_publication_receipt_sha256 is None
    assert completed.publication_receipt_sha256 == receipt.canonical_sha256()
    with (
        pytest.raises(CompareCurrentFileWebGraphConflictError, match="revision/state"),
        storage.transaction() as conn,
    ):
        complete_compare_current_file_web_work_graph_in_transaction(
            conn,
            graph_id=completed.id,
            user_id=completed.user_id,
            conversation_id=completed.conversation_id,
            expected_revision=completed.revision,
            publication_assistant_message_id=str(assistant["id"]),
            receipt=receipt,
            now=_next_instant(completed),
        )


def test_user_cancellation_is_exact_race_safe_rollback_atomic_and_not_replayable(storage) -> None:
    graph = _seed_graph(storage, "user-cancel")
    graph = _claim(storage, graph, FILE_READ_STEP_ID)
    with storage.transaction() as conn:
        cancelled = cancel_compare_current_file_web_work_graph_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
            expected_revision=graph.revision,
            now=_next_instant(graph),
        )
    assert cancelled.state is CompareCurrentFileWebGraphState.TERMINAL
    assert cancelled.outcome_status is CompareCurrentFileWebGraphOutcomeStatus.UNAVAILABLE
    assert cancelled.outcome_reason is CompareCurrentFileWebGraphOutcomeReason.CANCELLED
    assert cancelled.step(FILE_READ_STEP_ID).state is CompareCurrentFileWebStepState.RUNNING
    assistant = storage.get_message(
        str(cancelled.publication_assistant_message_id),
        cancelled.user_id,
    )
    assert assistant is not None
    assert assistant["content"] == COMPARE_CURRENT_FILE_WEB_CANCELLED_RESPONSE
    assert assistant["reply_to"] == graph.anchor_user_message_id
    receipt = load_compare_current_file_web_terminal_publication_receipt(str(assistant["metadata_json"]))
    assert receipt.reason is CompareCurrentFileWebGraphOutcomeReason.CANCELLED
    assert receipt.model_spoke is receipt.evidence_cited is False
    assert receipt.final_authority_rechecked is receipt.completion_claimed is False
    with (
        pytest.raises(CompareCurrentFileWebGraphConflictError, match="revision/state"),
        storage.transaction() as conn,
    ):
        claim_compare_current_file_web_step_in_transaction(
            conn,
            graph_id=cancelled.id,
            user_id=cancelled.user_id,
            conversation_id=cancelled.conversation_id,
            expected_revision=cancelled.revision,
            step_id=WEB_READ_STEP_ID,
            now=_next_instant(cancelled),
        )
    with (
        pytest.raises(CompareCurrentFileWebGraphConflictError, match="revision/state"),
        storage.transaction() as conn,
    ):
        cancel_compare_current_file_web_work_graph_in_transaction(
            conn,
            graph_id=cancelled.id,
            user_id=cancelled.user_id,
            conversation_id=cancelled.conversation_id,
            expected_revision=cancelled.revision,
            now=_next_instant(cancelled),
        )
    assert storage.count_messages(cancelled.conversation_id, user_id=cancelled.user_id) == 2

    raced = _seed_graph(storage, "user-cancel-race")
    for stale_user, stale_revision in (
        ("schema44-foreign-canceller", raced.revision),
        (raced.user_id, raced.revision + 1),
    ):
        with (
            pytest.raises(CompareCurrentFileWebGraphConflictError, match="revision/state"),
            storage.transaction() as conn,
        ):
            cancel_compare_current_file_web_work_graph_in_transaction(
                conn,
                graph_id=raced.id,
                user_id=stale_user,
                conversation_id=raced.conversation_id,
                expected_revision=stale_revision,
                now=_next_instant(raced),
            )
    assert storage.count_messages(raced.conversation_id, user_id=raced.user_id) == 1

    rolled_back = _seed_graph(storage, "user-cancel-rollback")
    trigger_name = "test_schema44_reject_graph_user_cancel_terminal"
    with storage.transaction() as conn:
        conn.execute(
            f"""CREATE TRIGGER {trigger_name}
                BEFORE UPDATE ON work_item_compare_current_file_web_graphs
                WHEN OLD.id='{rolled_back.id}' AND NEW.state='terminal'
                BEGIN SELECT RAISE(ABORT,'synthetic cancel failure'); END"""  # nosec B608
        )
    try:
        with (
            pytest.raises(sqlite3.IntegrityError, match="synthetic cancel failure"),
            storage.transaction() as conn,
        ):
            cancel_compare_current_file_web_work_graph_in_transaction(
                conn,
                graph_id=rolled_back.id,
                user_id=rolled_back.user_id,
                conversation_id=rolled_back.conversation_id,
                expected_revision=rolled_back.revision,
                now=_next_instant(rolled_back),
            )
    finally:
        with storage.transaction() as conn:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")  # nosec B608
    assert storage.count_messages(rolled_back.conversation_id, user_id=rolled_back.user_id) == 1
    with storage.transaction() as conn:
        active = get_current_compare_current_file_web_work_graph_in_transaction(
            conn,
            user_id=rolled_back.user_id,
            conversation_id=rolled_back.conversation_id,
        )
    assert active is not None
    assert active.id == rolled_back.id


def test_conversation_archive_retires_graph_with_one_atomic_code_owned_assistant(storage) -> None:
    graph = _seed_graph(storage, "conversation-archive")

    report = storage.delete_conversation(graph.conversation_id, graph.user_id)

    assert report["existed"] is report["archived"] is True
    assert report["messages_kept"] == 2
    assert report["cancelled"] == {
        "compare_current_file_web_graphs": 1,
        "work_items": 0,
    }
    archived_conversation = storage.get_conversation(graph.conversation_id, graph.user_id)
    assert archived_conversation is not None
    assert archived_conversation["is_archived"] == 1
    messages = storage.get_conversation_messages(
        graph.conversation_id,
        user_id=graph.user_id,
    )
    assert len(messages) == report["messages_kept"]
    assistant = messages[-1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == COMPARE_CURRENT_FILE_WEB_ARCHIVED_RESPONSE
    assert assistant["reply_to"] == graph.anchor_user_message_id
    receipt = load_compare_current_file_web_terminal_publication_receipt(str(assistant["metadata_json"]))
    assert receipt.reason is CompareCurrentFileWebGraphOutcomeReason.CONVERSATION_ARCHIVED
    assert receipt.model_spoke is receipt.evidence_cited is False
    assert receipt.final_authority_rechecked is receipt.completion_claimed is False
    with storage.transaction() as conn:
        retired = get_compare_current_file_web_work_graph_in_transaction(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
        )
        current = get_current_compare_current_file_web_work_graph_in_transaction(
            conn,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
        )
    assert retired is not None
    assert retired.state is CompareCurrentFileWebGraphState.TERMINAL
    assert retired.outcome_reason is CompareCurrentFileWebGraphOutcomeReason.CONVERSATION_ARCHIVED
    assert retired.publication_assistant_message_id == assistant["id"]
    assert current is None

    kinds = tuple(CompareCurrentFileWebStepKind)
    replacement = CompareCurrentFileWebWorkGraph.admitted(
        user_id=graph.user_id,
        conversation_id=graph.conversation_id,
        anchor_user_message_id=graph.anchor_user_message_id,
        current_file_raw_object_id=graph.current_file_raw_object_id,
        proposal_sha256=_sha256("archived-replacement-proposal"),
        accepted_plan_sha256=_sha256("archived-replacement-plan"),
        manifest_sha256=graph.manifest_sha256,
        policy_sha256=graph.policy_sha256,
        runtime_profile_sha256=graph.runtime_profile_sha256,
        adapter_registry_sha256=graph.adapter_registry_sha256,
        actor_binding_sha256=graph.actor_binding_sha256,
        conversation_binding_sha256=graph.conversation_binding_sha256,
        current_file_source_identity_sha256=graph.current_file_source_identity_sha256,
        current_file_content_sha256=graph.current_file_content_sha256,
        step_input_identities={kind: _sha256(f"archived-replacement:{kind}") for kind in kinds},
        step_idempotency_keys={kind: _sha256(f"archived-replacement-idempotency:{kind}") for kind in kinds},
        now="2026-08-26T11:00:00+00:00",
        expires_at="2026-08-26T22:00:00+00:00",
    )
    with (
        pytest.raises(CompareCurrentFileWebGraphAnchorError, match="archived conversation"),
        storage.transaction() as conn,
    ):
        create_compare_current_file_web_work_graph_in_transaction(conn, replacement)


def test_conversation_archive_retirement_is_owner_exact_race_safe_and_rollback_atomic(
    storage,
) -> None:
    foreign = _seed_graph(storage, "conversation-archive-foreign")
    assert storage.delete_conversation(foreign.conversation_id, "schema44-foreign-owner") == {
        "existed": False,
        "conversation_id": foreign.conversation_id,
    }
    assert storage.get_conversation(foreign.conversation_id, foreign.user_id)["is_archived"] == 0
    assert storage.count_messages(foreign.conversation_id, user_id=foreign.user_id) == 1

    raced = _seed_graph(storage, "conversation-archive-race")
    with (
        pytest.raises(CompareCurrentFileWebGraphConflictError, match="revision/state"),
        storage.transaction() as conn,
    ):
        conn.execute(
            "UPDATE conversations SET is_archived=1 WHERE id=? AND user_id=?",
            (raced.conversation_id, raced.user_id),
        )
        retire_compare_current_file_web_work_graph_for_archived_conversation_in_transaction(
            conn,
            graph_id=raced.id,
            user_id=raced.user_id,
            conversation_id=raced.conversation_id,
            expected_revision=raced.revision + 1,
            now=_next_instant(raced),
        )
    assert storage.get_conversation(raced.conversation_id, raced.user_id)["is_archived"] == 0
    assert storage.count_messages(raced.conversation_id, user_id=raced.user_id) == 1

    rolled_back = _seed_graph(storage, "conversation-archive-rollback")
    trigger_name = "test_schema44_reject_graph_archive_terminal"
    with storage.transaction() as conn:
        conn.execute(
            f"""CREATE TRIGGER {trigger_name}
                BEFORE UPDATE ON work_item_compare_current_file_web_graphs
                WHEN OLD.id='{rolled_back.id}' AND NEW.state='terminal'
                BEGIN SELECT RAISE(ABORT,'synthetic terminal failure'); END"""  # nosec B608
        )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="synthetic terminal failure"):
            storage.delete_conversation(rolled_back.conversation_id, rolled_back.user_id)
    finally:
        with storage.transaction() as conn:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")  # nosec B608
    assert storage.get_conversation(rolled_back.conversation_id, rolled_back.user_id)["is_archived"] == 0
    assert storage.count_messages(rolled_back.conversation_id, user_id=rolled_back.user_id) == 1
    with storage.transaction() as conn:
        active = get_current_compare_current_file_web_work_graph_in_transaction(
            conn,
            user_id=rolled_back.user_id,
            conversation_id=rolled_back.conversation_id,
        )
    assert active is not None
    assert active.id == rolled_back.id


def test_expiry_worker_seam_retires_restart_orphan_once_without_execution(settings, tmp_path) -> None:
    database = tmp_path / "schema44-expiry-restart.sqlite3"
    first = FridayStorage(replace(settings, database_path=database))
    graph = _seed_graph(
        first,
        "expiry-restart",
        now="2026-08-25T10:00:00+00:00",
        expires_at="2026-08-25T22:00:00+00:00",
    )
    first.close()

    reopened = FridayStorage(replace(settings, database_path=database, database_must_exist=True))
    try:
        with reopened.transaction() as conn:
            retired = expire_due_compare_current_file_web_work_graphs_in_transaction(
                conn,
                now="2026-08-26T10:00:00+00:00",
                limit=1,
            )
        assert len(retired) == 1
        terminal = retired[0]
        assert terminal.id == graph.id
        assert terminal.state is CompareCurrentFileWebGraphState.TERMINAL
        assert terminal.outcome_status is CompareCurrentFileWebGraphOutcomeStatus.UNAVAILABLE
        assert terminal.outcome_reason is CompareCurrentFileWebGraphOutcomeReason.EXPIRED
        assert terminal.publication_receipt_sha256 is None
        assistant = reopened.get_message(
            str(terminal.publication_assistant_message_id),
            terminal.user_id,
        )
        assert assistant is not None
        assert assistant["content"] == COMPARE_CURRENT_FILE_WEB_EXPIRED_RESPONSE
        assert assistant["reply_to"] == graph.anchor_user_message_id
        receipt = load_compare_current_file_web_terminal_publication_receipt(str(assistant["metadata_json"]))
        assert receipt.reason is CompareCurrentFileWebGraphOutcomeReason.EXPIRED
        assert receipt.model_spoke is receipt.evidence_cited is False
        assert receipt.final_authority_rechecked is receipt.completion_claimed is False
        with reopened.transaction() as conn:
            assert (
                get_current_compare_current_file_web_work_graph_in_transaction(
                    conn,
                    user_id=graph.user_id,
                    conversation_id=graph.conversation_id,
                )
                is None
            )
            assert (
                expire_due_compare_current_file_web_work_graphs_in_transaction(
                    conn,
                    now="2026-08-26T10:00:01+00:00",
                    limit=1,
                )
                == ()
            )
        assert reopened.count_messages(graph.conversation_id, user_id=graph.user_id) == 2
    finally:
        reopened.close()
