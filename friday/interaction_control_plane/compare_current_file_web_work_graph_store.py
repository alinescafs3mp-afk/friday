"""Transaction-only store for the fixed current-file/current-web WorkGraph.

The store advances durable structural state and nothing else.  It contains no
capability adapter, web client, model runtime, scheduler or automatic worker.
Callers must already own an SQLite transaction and all authority decisions.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from friday.orchestration.supervisor_review_policy import AdmittedReadRecovery

from friday.interaction_control_plane.compare_current_file_web_work_graph import (
    COMPARE_CURRENT_FILE_WEB_ARCHIVED_RESPONSE,
    COMPARE_CURRENT_FILE_WEB_CANCELLED_RESPONSE,
    COMPARE_CURRENT_FILE_WEB_COMPLETION_CONTRACT,
    COMPARE_CURRENT_FILE_WEB_EXPIRED_RESPONSE,
    COMPARE_CURRENT_FILE_WEB_FALLBACK_OWNER,
    COMPARE_CURRENT_FILE_WEB_MAX_ACTIVE_REVISION,
    COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS,
    COMPARE_CURRENT_FILE_WEB_PUBLICATION_OWNER,
    CompareCurrentFileWebGraphError,
    CompareCurrentFileWebGraphState,
    CompareCurrentFileWebGraphStep,
    CompareCurrentFileWebGraphTransition,
    CompareCurrentFileWebPublicationReceipt,
    CompareCurrentFileWebStepKind,
    CompareCurrentFileWebStepState,
    CompareCurrentFileWebTerminalPublicationReceipt,
    CompareCurrentFileWebWorkGraph,
    attach_compare_current_file_web_terminal_publication_receipt,
    load_compare_current_file_web_publication_receipt,
    load_compare_current_file_web_terminal_publication_receipt,
)
from friday.interaction_control_plane.work_item_contract import canonical_work_item_instant

_GRAPH_TABLE = "work_item_compare_current_file_web_graphs"
_STEP_TABLE = "work_item_compare_current_file_web_steps"
_SAVEPOINT = "compare_current_file_web_graph_mutation"


class CompareCurrentFileWebGraphConflictError(CompareCurrentFileWebGraphError):
    """The expected graph revision/state is no longer current."""


class CompareCurrentFileWebGraphAnchorError(CompareCurrentFileWebGraphError):
    """A durable source, conversation or publication anchor is not exact."""


def _require_transaction(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise RuntimeError("WorkGraph mutation requires an existing transaction")


def _row_mapping(cursor: sqlite3.Cursor, row: object) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    columns = tuple(str(item[0]) for item in cursor.description or ())
    if not isinstance(row, tuple) or len(row) != len(columns):
        raise CompareCurrentFileWebGraphError("stored WorkGraph row shape is invalid")
    return dict(zip(columns, row, strict=True))


def _fetch(
    conn: sqlite3.Connection,
    *,
    graph_id: str,
    user_id: str | None = None,
    conversation_id: str | None = None,
) -> CompareCurrentFileWebWorkGraph | None:
    clauses = ["id=?"]
    values: list[object] = [graph_id]
    if user_id is not None:
        clauses.append("user_id=?")
        values.append(user_id)
    if conversation_id is not None:
        clauses.append("conversation_id=?")
        values.append(conversation_id)
    cursor = conn.execute(
        f"SELECT * FROM {_GRAPH_TABLE} WHERE {' AND '.join(clauses)}",  # nosec B608 - fixed table/clauses
        tuple(values),
    )
    raw_graph = cursor.fetchone()
    if raw_graph is None:
        return None
    graph = _row_mapping(cursor, raw_graph)
    step_cursor = conn.execute(
        f"""SELECT * FROM {_STEP_TABLE}
              WHERE graph_id=?
              ORDER BY CASE kind
                         WHEN 'file_current_read' THEN 1
                         WHEN 'web_current_read' THEN 2
                         WHEN 'primary_synthesis' THEN 3
                         ELSE 4 END""",  # nosec B608 - fixed table
        (graph_id,),
    )
    step_rows = [_row_mapping(step_cursor, row) for row in step_cursor.fetchall()]
    return CompareCurrentFileWebWorkGraph.from_storage_rows(graph, step_rows)


def _validate_anchor(conn: sqlite3.Connection, graph: CompareCurrentFileWebWorkGraph) -> None:
    anchor = conn.execute(
        """SELECT 1
             FROM conversations conversation
             JOIN messages boundary
               ON boundary.id=?
              AND boundary.user_id=conversation.user_id
              AND boundary.conversation_id=conversation.id
              AND boundary.role='user'
             JOIN raw_objects source
               ON source.id=?
              AND source.user_id=conversation.user_id
            WHERE conversation.id=? AND conversation.user_id=?""",
        (
            graph.anchor_user_message_id,
            graph.current_file_raw_object_id,
            graph.conversation_id,
            graph.user_id,
        ),
    ).fetchone()
    if anchor is None:
        raise CompareCurrentFileWebGraphAnchorError("WorkGraph durable anchors are not owner-exact")


def _validate_publication(conn: sqlite3.Connection, graph: CompareCurrentFileWebWorkGraph) -> None:
    if graph.state is CompareCurrentFileWebGraphState.ACTIVE:
        return
    assert graph.publication_assistant_message_id is not None
    row = conn.execute(
        """SELECT metadata_json
             FROM messages
            WHERE id=? AND user_id=? AND conversation_id=?
              AND role='assistant' AND reply_to=?""",
        (
            graph.publication_assistant_message_id,
            graph.user_id,
            graph.conversation_id,
            graph.anchor_user_message_id,
        ),
    ).fetchone()
    if row is None:
        raise CompareCurrentFileWebGraphAnchorError("WorkGraph publication assistant is not exact")
    raw_metadata = row["metadata_json"] if isinstance(row, sqlite3.Row) else row[0]
    if graph.state is CompareCurrentFileWebGraphState.TERMINAL:
        assert graph.terminal_publication_receipt_sha256 is not None
        terminal_receipt = load_compare_current_file_web_terminal_publication_receipt(
            str(raw_metadata or "{}")
        )
        if (
            terminal_receipt.graph_id != graph.id
            or terminal_receipt.terminal_revision != graph.revision
            or terminal_receipt.accepted_plan_sha256 != graph.accepted_plan_sha256
            or terminal_receipt.status is not graph.outcome_status
            or terminal_receipt.reason is not graph.outcome_reason
            or terminal_receipt.graph_outcome_sha256 != graph.accepted_graph_outcome_sha256
            or terminal_receipt.steps_sha256 != graph.accepted_steps_sha256
            or terminal_receipt.canonical_sha256() != graph.terminal_publication_receipt_sha256
        ):
            raise CompareCurrentFileWebGraphAnchorError("WorkGraph terminal publication receipt is not exact")
        return
    assert graph.publication_receipt_sha256 is not None
    receipt = load_compare_current_file_web_publication_receipt(str(raw_metadata or "{}"))
    if (
        receipt.graph_id != graph.id
        or receipt.completed_revision != graph.revision
        or receipt.accepted_plan_sha256 != graph.accepted_plan_sha256
        or receipt.graph_outcome_sha256 != graph.accepted_graph_outcome_sha256
        or receipt.steps_sha256 != graph.accepted_steps_sha256
        or receipt.canonical_sha256() != graph.publication_receipt_sha256
    ):
        raise CompareCurrentFileWebGraphAnchorError("WorkGraph publication receipt is not exact")


def _validate_stored_graph(conn: sqlite3.Connection, graph: CompareCurrentFileWebWorkGraph) -> None:
    _validate_anchor(conn, graph)
    if len(graph.steps) != 3:
        raise CompareCurrentFileWebGraphError("stored WorkGraph does not have exactly three steps")
    if graph.state is CompareCurrentFileWebGraphState.ACTIVE:
        conflicting = conn.execute(
            """SELECT 1 FROM work_items
                WHERE user_id=? AND conversation_id=?
                  AND state IN ('active','waiting_for_input')
                LIMIT 1""",
            (graph.user_id, graph.conversation_id),
        ).fetchone()
        if conflicting is not None:
            raise CompareCurrentFileWebGraphConflictError("another durable Work Item owns the conversation")
    _validate_publication(conn, graph)


def _begin_savepoint(conn: sqlite3.Connection) -> None:
    conn.execute(f"SAVEPOINT {_SAVEPOINT}")


def _rollback_savepoint(conn: sqlite3.Connection) -> None:
    conn.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
    conn.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")


def _release_savepoint(conn: sqlite3.Connection) -> None:
    conn.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")


def _logical_now(
    value: str | None,
    *,
    graph: CompareCurrentFileWebWorkGraph,
    allow_expired_retirement: bool = False,
) -> str:
    timestamp = canonical_work_item_instant(
        value or datetime.now(UTC).isoformat(),
        label="now",
    )
    if timestamp < graph.updated_at:
        raise CompareCurrentFileWebGraphConflictError("WorkGraph time cannot move backwards")
    if not allow_expired_retirement and timestamp >= graph.expires_at:
        raise CompareCurrentFileWebGraphConflictError("WorkGraph deadline has expired")
    return timestamp


def _require_active_revision_available(graph: CompareCurrentFileWebWorkGraph) -> None:
    if graph.revision >= COMPARE_CURRENT_FILE_WEB_MAX_ACTIVE_REVISION:
        raise CompareCurrentFileWebGraphConflictError(
            "WorkGraph must use its reserved final revision for deterministic retirement"
        )


def _insert_graph(conn: sqlite3.Connection, graph: CompareCurrentFileWebWorkGraph) -> None:
    conn.execute(
        f"""INSERT INTO {_GRAPH_TABLE}(
               id,user_id,conversation_id,anchor_user_message_id,current_file_raw_object_id,
               state,revision,transition,proposal_sha256,accepted_plan_sha256,manifest_sha256,
               policy_sha256,runtime_profile_sha256,adapter_registry_sha256,actor_binding_sha256,
               conversation_binding_sha256,current_file_source_identity_sha256,
               current_file_content_sha256,completion_contract,fallback_owner,publication_owner,
               max_attempts,created_at,updated_at,expires_at,closed_at,
               outcome_status,outcome_reason,publication_assistant_message_id,accepted_graph_outcome_sha256,
               accepted_steps_sha256,terminal_publication_receipt_sha256,
               publication_receipt_sha256
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",  # nosec B608
        (
            graph.id,
            graph.user_id,
            graph.conversation_id,
            graph.anchor_user_message_id,
            graph.current_file_raw_object_id,
            graph.state.value,
            graph.revision,
            graph.transition.value,
            graph.proposal_sha256,
            graph.accepted_plan_sha256,
            graph.manifest_sha256,
            graph.policy_sha256,
            graph.runtime_profile_sha256,
            graph.adapter_registry_sha256,
            graph.actor_binding_sha256,
            graph.conversation_binding_sha256,
            graph.current_file_source_identity_sha256,
            graph.current_file_content_sha256,
            COMPARE_CURRENT_FILE_WEB_COMPLETION_CONTRACT,
            COMPARE_CURRENT_FILE_WEB_FALLBACK_OWNER,
            COMPARE_CURRENT_FILE_WEB_PUBLICATION_OWNER,
            COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS,
            graph.created_at,
            graph.updated_at,
            graph.expires_at,
            graph.closed_at,
            None if graph.outcome_status is None else graph.outcome_status.value,
            None if graph.outcome_reason is None else graph.outcome_reason.value,
            graph.publication_assistant_message_id,
            graph.accepted_graph_outcome_sha256,
            graph.accepted_steps_sha256,
            graph.terminal_publication_receipt_sha256,
            graph.publication_receipt_sha256,
        ),
    )
    conn.executemany(
        f"""INSERT INTO {_STEP_TABLE}(
               graph_id,step_id,kind,capability_id,security_id,adapter_id,effect_class,
               evidence_replayability,depends_on_json,
               parallel_group,input_identity_sha256,idempotency_key_sha256,state,attempt,
               outcome_schema,outcome_sha256,prior_outcome_sha256,
               recovery_review_sha256,recovery_context_sha256,
               evidence_identity_sha256,authority_rechecked,verified,started_at,settled_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",  # nosec B608
        tuple(
            (
                step.graph_id,
                step.step_id,
                step.kind.value,
                step.capability_id,
                step.security_id,
                step.adapter_id,
                "read",
                step.evidence_replayability,
                json.dumps(list(step.depends_on), ensure_ascii=True, separators=(",", ":")),
                step.parallel_group,
                step.input_identity_sha256,
                step.idempotency_key_sha256,
                step.state.value,
                step.attempt,
                "friday.compare-current-file-with-current-web-step-outcome.v1",
                step.outcome_sha256,
                step.prior_outcome_sha256,
                step.recovery_review_sha256,
                step.recovery_context_sha256,
                step.evidence_identity_sha256,
                int(step.authority_rechecked),
                int(step.verified),
                step.started_at,
                step.settled_at,
            )
            for step in graph.steps
        ),
    )


def create_compare_current_file_web_work_graph_in_transaction(
    conn: sqlite3.Connection,
    graph: CompareCurrentFileWebWorkGraph,
) -> CompareCurrentFileWebWorkGraph:
    """Persist exactly one pristine admitted fixed graph."""

    _require_transaction(conn)
    if type(graph) is not CompareCurrentFileWebWorkGraph:
        raise CompareCurrentFileWebGraphError("create requires the exact WorkGraph contract")
    if (
        graph.state is not CompareCurrentFileWebGraphState.ACTIVE
        or graph.transition is not CompareCurrentFileWebGraphTransition.ADMITTED
        or graph.revision != 1
        or any(step.state is not CompareCurrentFileWebStepState.PENDING for step in graph.steps)
    ):
        raise CompareCurrentFileWebGraphError("only a pristine admitted WorkGraph may be created")
    _validate_anchor(conn, graph)
    archived = conn.execute(
        "SELECT is_archived FROM conversations WHERE id=? AND user_id=?",
        (graph.conversation_id, graph.user_id),
    ).fetchone()
    if archived is None or int(archived[0]) != 0:
        raise CompareCurrentFileWebGraphAnchorError(
            "WorkGraph cannot be admitted into an archived conversation"
        )
    _begin_savepoint(conn)
    try:
        _insert_graph(conn, graph)
        stored = _fetch(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
        )
        if stored is None:  # pragma: no cover - same transaction inserted it
            raise CompareCurrentFileWebGraphConflictError("WorkGraph insert did not become durable")
        _validate_stored_graph(conn, stored)
    except BaseException as exc:
        _rollback_savepoint(conn)
        if isinstance(exc, sqlite3.IntegrityError):
            raise CompareCurrentFileWebGraphConflictError(
                "WorkGraph admission lost its ownership race"
            ) from exc
        raise
    _release_savepoint(conn)
    return stored


def get_compare_current_file_web_work_graph_in_transaction(
    conn: sqlite3.Connection,
    *,
    graph_id: str,
    user_id: str,
    conversation_id: str,
) -> CompareCurrentFileWebWorkGraph | None:
    graph = _fetch(
        conn,
        graph_id=graph_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    if graph is not None:
        _validate_stored_graph(conn, graph)
    return graph


def get_current_compare_current_file_web_work_graph_in_transaction(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    conversation_id: str,
) -> CompareCurrentFileWebWorkGraph | None:
    cursor = conn.execute(
        f"""SELECT id FROM {_GRAPH_TABLE}
              WHERE user_id=? AND conversation_id=? AND state='active'
              ORDER BY updated_at DESC,id DESC LIMIT 2""",  # nosec B608
        (user_id, conversation_id),
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise CompareCurrentFileWebGraphConflictError("multiple active WorkGraphs own one conversation")
    if not rows:
        return None
    row = rows[0]
    graph_id = str(row["id"] if isinstance(row, sqlite3.Row) else row[0])
    return get_compare_current_file_web_work_graph_in_transaction(
        conn,
        graph_id=graph_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )


def _current_for_mutation(
    conn: sqlite3.Connection,
    *,
    graph_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
) -> CompareCurrentFileWebWorkGraph:
    graph = get_compare_current_file_web_work_graph_in_transaction(
        conn,
        graph_id=graph_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    if (
        graph is None
        or graph.state is not CompareCurrentFileWebGraphState.ACTIVE
        or graph.revision != expected_revision
    ):
        raise CompareCurrentFileWebGraphConflictError("WorkGraph revision/state is no longer current")
    return graph


def _dependencies_complete(
    graph: CompareCurrentFileWebWorkGraph, step: CompareCurrentFileWebGraphStep
) -> bool:
    if step.kind is not CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS:
        return not step.depends_on
    reads = tuple(graph.step(dependency) for dependency in step.depends_on)
    return bool(
        all(item.settled for item in reads)
        and any(
            item.state
            in {
                CompareCurrentFileWebStepState.COMPLETE,
                CompareCurrentFileWebStepState.PARTIAL,
            }
            for item in reads
        )
    )


def claim_compare_current_file_web_step_in_transaction(
    conn: sqlite3.Connection,
    *,
    graph_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    step_id: str,
    now: str | None = None,
) -> CompareCurrentFileWebWorkGraph:
    """CAS one ready structural step from pending to running.

    Claiming does not execute anything.  A future admitted runtime must perform
    adapter and authority checks independently after this durable boundary.
    """

    _require_transaction(conn)
    graph = _current_for_mutation(
        conn,
        graph_id=graph_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
    )
    _require_active_revision_available(graph)
    step = graph.step(step_id)
    if (
        step.state is not CompareCurrentFileWebStepState.PENDING
        or step.attempt >= COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS
        or not _dependencies_complete(graph, step)
    ):
        raise CompareCurrentFileWebGraphConflictError("WorkGraph step is not ready to claim")
    timestamp = _logical_now(now, graph=graph)
    next_step = replace(
        step,
        state=CompareCurrentFileWebStepState.RUNNING,
        attempt=step.attempt + 1,
        started_at=timestamp,
    )
    _begin_savepoint(conn)
    try:
        graph_cursor = conn.execute(
            f"""UPDATE {_GRAPH_TABLE}
                   SET revision=revision+1,transition='step_claimed',updated_at=?
                 WHERE id=? AND user_id=? AND conversation_id=?
                   AND state='active' AND revision=? AND expires_at>?""",  # nosec B608
            (timestamp, graph.id, graph.user_id, graph.conversation_id, graph.revision, timestamp),
        )
        step_cursor = conn.execute(
            f"""UPDATE {_STEP_TABLE}
                   SET state='running',attempt=?,started_at=?
                 WHERE graph_id=? AND step_id=? AND state='pending' AND attempt=?""",  # nosec B608
            (next_step.attempt, timestamp, graph.id, step.step_id, step.attempt),
        )
        if graph_cursor.rowcount != 1 or step_cursor.rowcount != 1:
            raise CompareCurrentFileWebGraphConflictError("WorkGraph step claim lost its CAS race")
        updated = _fetch(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
        )
        if updated is None:  # pragma: no cover
            raise CompareCurrentFileWebGraphConflictError("claimed WorkGraph disappeared")
        _validate_stored_graph(conn, updated)
    except BaseException:
        _rollback_savepoint(conn)
        raise
    _release_savepoint(conn)
    return updated


def settle_compare_current_file_web_step_in_transaction(
    conn: sqlite3.Connection,
    *,
    graph_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    step_id: str,
    state: CompareCurrentFileWebStepState,
    outcome_sha256: str,
    evidence_identity_sha256: str | None,
    authority_rechecked: bool,
    verified: bool,
    now: str | None = None,
) -> CompareCurrentFileWebWorkGraph:
    """CAS one running step to one typed structural terminal outcome."""

    _require_transaction(conn)
    graph = _current_for_mutation(
        conn,
        graph_id=graph_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
    )
    _require_active_revision_available(graph)
    step = graph.step(step_id)
    if step.state is not CompareCurrentFileWebStepState.RUNNING:
        raise CompareCurrentFileWebGraphConflictError("only a running WorkGraph step may settle")
    if not isinstance(state, CompareCurrentFileWebStepState) or state in {
        CompareCurrentFileWebStepState.PENDING,
        CompareCurrentFileWebStepState.RUNNING,
    }:
        raise CompareCurrentFileWebGraphError("step terminal state must use the closed outcome vocabulary")
    timestamp = _logical_now(now, graph=graph)
    next_step = replace(
        step,
        state=state,
        outcome_sha256=outcome_sha256,
        evidence_identity_sha256=evidence_identity_sha256,
        authority_rechecked=authority_rechecked,
        verified=verified,
        settled_at=timestamp,
    )
    if step.kind is CompareCurrentFileWebStepKind.PRIMARY_SYNTHESIS:
        reads = graph.steps[:2]
        reads_complete = all(item.state is CompareCurrentFileWebStepState.COMPLETE for item in reads)
        has_usable_read = any(
            item.state
            in {
                CompareCurrentFileWebStepState.COMPLETE,
                CompareCurrentFileWebStepState.PARTIAL,
            }
            for item in reads
        )
        if state is CompareCurrentFileWebStepState.COMPLETE and not reads_complete:
            raise CompareCurrentFileWebGraphError(
                "primary synthesis cannot claim complete comparison from incomplete reads"
            )
        if state is CompareCurrentFileWebStepState.PARTIAL and not has_usable_read:
            raise CompareCurrentFileWebGraphError(
                "primary partial fallback requires usable process-owned read evidence"
            )
    _begin_savepoint(conn)
    try:
        graph_cursor = conn.execute(
            f"""UPDATE {_GRAPH_TABLE}
                   SET revision=revision+1,transition='step_settled',updated_at=?
                 WHERE id=? AND user_id=? AND conversation_id=?
                   AND state='active' AND revision=? AND expires_at>?""",  # nosec B608
            (timestamp, graph.id, graph.user_id, graph.conversation_id, graph.revision, timestamp),
        )
        step_cursor = conn.execute(
            f"""UPDATE {_STEP_TABLE}
                   SET state=?,outcome_sha256=?,evidence_identity_sha256=?,
                       authority_rechecked=?,verified=?,settled_at=?
                 WHERE graph_id=? AND step_id=? AND state='running' AND attempt=?""",  # nosec B608
            (
                next_step.state.value,
                next_step.outcome_sha256,
                next_step.evidence_identity_sha256,
                int(next_step.authority_rechecked),
                int(next_step.verified),
                timestamp,
                graph.id,
                step.step_id,
                step.attempt,
            ),
        )
        if graph_cursor.rowcount != 1 or step_cursor.rowcount != 1:
            raise CompareCurrentFileWebGraphConflictError("WorkGraph step settlement lost its CAS race")
        updated = _fetch(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
        )
        if updated is None:  # pragma: no cover
            raise CompareCurrentFileWebGraphConflictError("settled WorkGraph disappeared")
        _validate_stored_graph(conn, updated)
    except BaseException:
        _rollback_savepoint(conn)
        raise
    _release_savepoint(conn)
    return updated


def requeue_interrupted_compare_current_file_web_step_in_transaction(
    conn: sqlite3.Connection,
    *,
    graph_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    step_id: str,
    now: str | None = None,
) -> CompareCurrentFileWebWorkGraph:
    """Return one interrupted effect-free step to pending without changing identity."""

    _require_transaction(conn)
    graph = _current_for_mutation(
        conn,
        graph_id=graph_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
    )
    _require_active_revision_available(graph)
    step = graph.step(step_id)
    if (
        step.state is not CompareCurrentFileWebStepState.RUNNING
        or step.attempt >= COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS
    ):
        raise CompareCurrentFileWebGraphConflictError("WorkGraph step is not eligible for bounded requeue")
    timestamp = _logical_now(now, graph=graph)
    replace(
        step,
        state=CompareCurrentFileWebStepState.PENDING,
        started_at=None,
    )
    _begin_savepoint(conn)
    try:
        graph_cursor = conn.execute(
            f"""UPDATE {_GRAPH_TABLE}
                   SET revision=revision+1,transition='step_requeued',updated_at=?
                 WHERE id=? AND user_id=? AND conversation_id=?
                   AND state='active' AND revision=? AND expires_at>?""",  # nosec B608
            (timestamp, graph.id, graph.user_id, graph.conversation_id, graph.revision, timestamp),
        )
        step_cursor = conn.execute(
            f"""UPDATE {_STEP_TABLE}
                   SET state='pending',started_at=NULL
                 WHERE graph_id=? AND step_id=? AND state='running' AND attempt=?""",  # nosec B608
            (graph.id, step.step_id, step.attempt),
        )
        if graph_cursor.rowcount != 1 or step_cursor.rowcount != 1:
            raise CompareCurrentFileWebGraphConflictError("WorkGraph requeue lost its CAS race")
        updated = _fetch(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
        )
        if updated is None:  # pragma: no cover
            raise CompareCurrentFileWebGraphConflictError("requeued WorkGraph disappeared")
        _validate_stored_graph(conn, updated)
    except BaseException:
        _rollback_savepoint(conn)
        raise
    _release_savepoint(conn)
    return updated


def admit_compare_current_file_web_review_recovery_in_transaction(
    conn: sqlite3.Connection,
    *,
    graph_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    recovery: AdmittedReadRecovery,
    now: str | None = None,
) -> CompareCurrentFileWebWorkGraph:
    """CAS the one policy-admitted failed current-web read back to pending.

    The helper executes nothing.  It accepts only the exact code-owned P4
    admission witness for the fixed current-web read, seals its review/context
    digests durably, and consumes the step's second attempt on the next claim.
    """

    from friday.orchestration.supervisor_contracts import CompletionCriterion
    from friday.orchestration.supervisor_review_policy import AdmittedReadRecovery

    _require_transaction(conn)
    if type(recovery) is not AdmittedReadRecovery:
        raise CompareCurrentFileWebGraphError(
            "review recovery requires the exact code-owned admission witness"
        )
    graph = _current_for_mutation(
        conn,
        graph_id=graph_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
    )
    _require_active_revision_available(graph)
    step = graph.step("read_current_web")
    if (
        step.state
        not in {
            CompareCurrentFileWebStepState.EMPTY,
            CompareCurrentFileWebStepState.UNAVAILABLE,
            CompareCurrentFileWebStepState.FAILED,
        }
        or step.attempt >= COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS
        or step.recovery_review_sha256 is not None
        or step.recovery_context_sha256 is not None
    ):
        raise CompareCurrentFileWebGraphConflictError(
            "current-web read is not eligible for one bounded review recovery"
        )
    if (
        recovery.step_id != step.step_id
        or recovery.capability_id != step.capability_id
        or recovery.criterion is not CompletionCriterion.CURRENT_PUBLIC_EVIDENCE_HAS_COVERAGE
        or recovery.idempotency_key != step.idempotency_key_sha256
    ):
        raise CompareCurrentFileWebGraphAnchorError(
            "review recovery witness does not match the fixed current-web read"
        )
    next_step = replace(
        step,
        state=CompareCurrentFileWebStepState.PENDING,
        outcome_sha256=None,
        prior_outcome_sha256=step.outcome_sha256,
        recovery_review_sha256=recovery.review_digest,
        recovery_context_sha256=recovery.context_digest,
        evidence_identity_sha256=None,
        authority_rechecked=False,
        verified=False,
        started_at=None,
        settled_at=None,
    )
    timestamp = _logical_now(now, graph=graph)
    _begin_savepoint(conn)
    try:
        graph_cursor = conn.execute(
            f"""UPDATE {_GRAPH_TABLE}
                   SET revision=revision+1,transition='review_recovery_admitted',updated_at=?
                 WHERE id=? AND user_id=? AND conversation_id=?
                   AND state='active' AND revision=? AND expires_at>?""",  # nosec B608
            (timestamp, graph.id, graph.user_id, graph.conversation_id, graph.revision, timestamp),
        )
        step_cursor = conn.execute(
            f"""UPDATE {_STEP_TABLE}
                   SET state='pending',outcome_sha256=NULL,
                       prior_outcome_sha256=?,recovery_review_sha256=?,
                       recovery_context_sha256=?,evidence_identity_sha256=NULL,
                       authority_rechecked=0,verified=0,started_at=NULL,settled_at=NULL
                 WHERE graph_id=? AND step_id='read_current_web'
                   AND kind='web_current_read' AND state=? AND attempt=?
                   AND outcome_sha256=? AND prior_outcome_sha256 IS NULL
                   AND recovery_review_sha256 IS NULL
                   AND recovery_context_sha256 IS NULL""",  # nosec B608
            (
                next_step.prior_outcome_sha256,
                next_step.recovery_review_sha256,
                next_step.recovery_context_sha256,
                graph.id,
                step.state.value,
                step.attempt,
                step.outcome_sha256,
            ),
        )
        if graph_cursor.rowcount != 1 or step_cursor.rowcount != 1:
            raise CompareCurrentFileWebGraphConflictError("current-web review recovery lost its CAS race")
        updated = _fetch(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
        )
        if updated is None:  # pragma: no cover
            raise CompareCurrentFileWebGraphConflictError("recovered WorkGraph disappeared")
        _validate_stored_graph(conn, updated)
    except BaseException:
        _rollback_savepoint(conn)
        raise
    _release_savepoint(conn)
    return updated


def prepare_compare_current_file_web_restart_rebind_in_transaction(
    conn: sqlite3.Connection,
    *,
    graph_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    now: str | None = None,
) -> CompareCurrentFileWebWorkGraph:
    """Invalidate process-private payload digests before restart synthesis.

    The durable digests prove identity only; they are never treated as replayable
    evidence bodies. Accepted reads (and an uncommitted accepted synthesis) move
    back to pending with their prior outcome identity retained. A later claim
    consumes the second attempt and must settle reads with fresh authority proof.
    """

    _require_transaction(conn)
    graph = _current_for_mutation(
        conn,
        graph_id=graph_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
    )
    _require_active_revision_available(graph)
    reads = graph.steps[:2]
    synthesis = graph.steps[2]
    if not all(step.settled for step in reads):
        raise CompareCurrentFileWebGraphConflictError(
            "restart rebind requires both read attempts to have settled"
        )
    accepted_states = {
        CompareCurrentFileWebStepState.COMPLETE,
        CompareCurrentFileWebStepState.PARTIAL,
        CompareCurrentFileWebStepState.EMPTY,
    }
    accepted = tuple(step for step in graph.steps if step.state in accepted_states)
    if not accepted:
        raise CompareCurrentFileWebGraphConflictError(
            "restart rebind has no process-private accepted payload"
        )
    if any(step.attempt >= COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS for step in accepted) or (
        synthesis.state is CompareCurrentFileWebStepState.RUNNING
        and synthesis.attempt >= COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS
    ):
        raise CompareCurrentFileWebGraphConflictError(
            "process-private evidence exhausted its bounded restart rebind"
        )
    if synthesis.state not in {
        CompareCurrentFileWebStepState.PENDING,
        CompareCurrentFileWebStepState.RUNNING,
        CompareCurrentFileWebStepState.COMPLETE,
        CompareCurrentFileWebStepState.PARTIAL,
    }:
        raise CompareCurrentFileWebGraphConflictError(
            "settled failed synthesis must close honestly instead of replaying"
        )
    timestamp = _logical_now(now, graph=graph)
    _begin_savepoint(conn)
    try:
        graph_cursor = conn.execute(
            f"""UPDATE {_GRAPH_TABLE}
                   SET revision=revision+1,transition='restart_rebind',updated_at=?
                 WHERE id=? AND user_id=? AND conversation_id=?
                   AND state='active' AND revision=? AND expires_at>?""",  # nosec B608
            (timestamp, graph.id, graph.user_id, graph.conversation_id, graph.revision, timestamp),
        )
        changed = 0
        for step in accepted:
            cursor = conn.execute(
                f"""UPDATE {_STEP_TABLE}
                       SET state='pending',prior_outcome_sha256=outcome_sha256,
                           outcome_sha256=NULL,evidence_identity_sha256=NULL,
                           authority_rechecked=0,verified=0,started_at=NULL,settled_at=NULL
                     WHERE graph_id=? AND step_id=? AND state=? AND attempt=?
                       AND outcome_sha256=?""",  # nosec B608
                (
                    graph.id,
                    step.step_id,
                    step.state.value,
                    step.attempt,
                    step.outcome_sha256,
                ),
            )
            changed += cursor.rowcount
        if synthesis.state is CompareCurrentFileWebStepState.RUNNING:
            cursor = conn.execute(
                f"""UPDATE {_STEP_TABLE}
                       SET state='pending',started_at=NULL
                     WHERE graph_id=? AND step_id=? AND state='running' AND attempt=?""",  # nosec B608
                (graph.id, synthesis.step_id, synthesis.attempt),
            )
            changed += cursor.rowcount
        expected_changes = len(accepted) + int(synthesis.state is CompareCurrentFileWebStepState.RUNNING)
        if graph_cursor.rowcount != 1 or changed != expected_changes:
            raise CompareCurrentFileWebGraphConflictError("WorkGraph restart rebind lost its CAS race")
        updated = _fetch(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
        )
        if updated is None:  # pragma: no cover
            raise CompareCurrentFileWebGraphConflictError("rebound WorkGraph disappeared")
        _validate_stored_graph(conn, updated)
    except BaseException:
        _rollback_savepoint(conn)
        raise
    _release_savepoint(conn)
    return updated


def close_compare_current_file_web_work_graph_terminal_in_transaction(
    conn: sqlite3.Connection,
    *,
    graph_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    publication_assistant_message_id: str,
    receipt: CompareCurrentFileWebTerminalPublicationReceipt,
    evidence_not_replayable: bool = False,
    conversation_archived: bool = False,
    cancelled: bool = False,
    expired: bool = False,
    now: str | None = None,
) -> CompareCurrentFileWebWorkGraph:
    """Atomically publish one honest terminal fallback without claiming completion."""

    _require_transaction(conn)
    graph = _current_for_mutation(
        conn,
        graph_id=graph_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
    )
    if type(receipt) is not CompareCurrentFileWebTerminalPublicationReceipt:
        raise CompareCurrentFileWebGraphError("terminal closure requires the exact publication receipt")
    expected_receipt = graph.terminal_publication_receipt(
        evidence_not_replayable=evidence_not_replayable,
        conversation_archived=conversation_archived,
        cancelled=cancelled,
        expired=expired,
        final_authority_rechecked=receipt.final_authority_rechecked,
    )
    if receipt != expected_receipt:
        raise CompareCurrentFileWebGraphAnchorError(
            "terminal publication receipt does not match current graph state"
        )
    status, reason = receipt.status, receipt.reason
    timestamp = _logical_now(
        now,
        graph=graph,
        allow_expired_retirement=conversation_archived or cancelled or expired,
    )
    if expired and timestamp < graph.expires_at:
        raise CompareCurrentFileWebGraphConflictError("WorkGraph cannot expire before its exact deadline")
    if conversation_archived:
        archived = conn.execute(
            """SELECT 1 FROM conversations
                WHERE id=? AND user_id=? AND is_archived=1""",
            (graph.conversation_id, graph.user_id),
        ).fetchone()
        if archived is None:
            raise CompareCurrentFileWebGraphAnchorError(
                "conversation archive retirement requires the exact archived owner"
            )
    assistant = conn.execute(
        """SELECT content,metadata_json FROM messages
            WHERE id=? AND user_id=? AND conversation_id=?
              AND role='assistant' AND reply_to=?""",
        (
            publication_assistant_message_id,
            graph.user_id,
            graph.conversation_id,
            graph.anchor_user_message_id,
        ),
    ).fetchone()
    if assistant is None:
        raise CompareCurrentFileWebGraphAnchorError(
            "terminal publication assistant is outside the graph boundary"
        )
    if isinstance(assistant, sqlite3.Row):
        assistant_content = str(assistant["content"])
        metadata_json = assistant["metadata_json"]
    else:
        assistant_content = str(assistant[0])
        metadata_json = assistant[1]
    expected_content = (
        COMPARE_CURRENT_FILE_WEB_ARCHIVED_RESPONSE
        if conversation_archived
        else COMPARE_CURRENT_FILE_WEB_CANCELLED_RESPONSE
        if cancelled
        else COMPARE_CURRENT_FILE_WEB_EXPIRED_RESPONSE
        if expired
        else None
    )
    if expected_content is not None and assistant_content != expected_content:
        raise CompareCurrentFileWebGraphAnchorError(
            "deterministic retirement assistant content is not code-owned"
        )
    load_compare_current_file_web_terminal_publication_receipt(
        str(metadata_json or "{}"),
        expected=receipt,
    )
    receipt_sha256 = receipt.canonical_sha256()
    _begin_savepoint(conn)
    try:
        cursor = conn.execute(
            f"""UPDATE {_GRAPH_TABLE}
                   SET state='terminal',revision=revision+1,transition='terminal_settled',
                       updated_at=?,closed_at=?,outcome_status=?,outcome_reason=?,
                       publication_assistant_message_id=?,accepted_graph_outcome_sha256=?,
                       accepted_steps_sha256=?,terminal_publication_receipt_sha256=?
                 WHERE id=? AND user_id=? AND conversation_id=?
                   AND state='active' AND revision=?
                   AND (?=1 OR expires_at>?)""",  # nosec B608
            (
                timestamp,
                timestamp,
                status.value,
                reason.value,
                publication_assistant_message_id,
                receipt.graph_outcome_sha256,
                receipt.steps_sha256,
                receipt_sha256,
                graph.id,
                graph.user_id,
                graph.conversation_id,
                graph.revision,
                int(conversation_archived or cancelled or expired),
                timestamp,
            ),
        )
        if cursor.rowcount != 1:
            raise CompareCurrentFileWebGraphConflictError("WorkGraph terminal closure lost its CAS race")
        terminal = _fetch(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
        )
        if terminal is None:  # pragma: no cover
            raise CompareCurrentFileWebGraphConflictError("terminal WorkGraph disappeared")
        _validate_stored_graph(conn, terminal)
    except BaseException:
        _rollback_savepoint(conn)
        raise
    _release_savepoint(conn)
    return terminal


def _publish_deterministic_retirement(
    conn: sqlite3.Connection,
    *,
    graph: CompareCurrentFileWebWorkGraph,
    content: str,
    conversation_archived: bool,
    cancelled: bool,
    expired: bool,
    now: str,
) -> CompareCurrentFileWebWorkGraph:
    from friday.storage._conversations import store_message_in_transaction

    receipt = graph.terminal_publication_receipt(
        conversation_archived=conversation_archived,
        cancelled=cancelled,
        expired=expired,
        final_authority_rechecked=False,
    )
    metadata = attach_compare_current_file_web_terminal_publication_receipt({}, receipt)
    savepoint = "compare_current_file_web_retirement"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        assistant = store_message_in_transaction(
            conn,
            graph.conversation_id,
            graph.user_id,
            "assistant",
            content,
            metadata,
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
            conversation_archived=conversation_archived,
            cancelled=cancelled,
            expired=expired,
            now=now,
        )
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    return terminal


def retire_compare_current_file_web_work_graph_for_archived_conversation_in_transaction(
    conn: sqlite3.Connection,
    *,
    graph_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    now: str | None = None,
) -> CompareCurrentFileWebWorkGraph:
    """Publish the one code-owned archive response and release ownership."""

    _require_transaction(conn)
    graph = _current_for_mutation(
        conn,
        graph_id=graph_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
    )
    timestamp = canonical_work_item_instant(
        now or datetime.now(UTC).isoformat(),
        label="now",
    )
    timestamp = max(timestamp, graph.updated_at)
    return _publish_deterministic_retirement(
        conn,
        graph=graph,
        content=COMPARE_CURRENT_FILE_WEB_ARCHIVED_RESPONSE,
        conversation_archived=True,
        cancelled=False,
        expired=False,
        now=timestamp,
    )


def expire_due_compare_current_file_web_work_graphs_in_transaction(
    conn: sqlite3.Connection,
    *,
    now: str | None = None,
    limit: int = 100,
) -> tuple[CompareCurrentFileWebWorkGraph, ...]:
    """Bounded worker seam for deterministic expiry publication; it executes no capability."""

    _require_transaction(conn)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise CompareCurrentFileWebGraphError("expiry retirement limit must be between 1 and 100")
    timestamp = canonical_work_item_instant(
        now or datetime.now(UTC).isoformat(),
        label="now",
    )
    rows = conn.execute(
        f"""SELECT id,user_id,conversation_id,revision
              FROM {_GRAPH_TABLE}
             WHERE state='active' AND expires_at<=?
             ORDER BY expires_at,id
             LIMIT ?""",  # nosec B608 - fixed table
        (timestamp, limit),
    ).fetchall()
    retired: list[CompareCurrentFileWebWorkGraph] = []
    for row in rows:
        item = (
            dict(row)
            if isinstance(row, sqlite3.Row)
            else {
                "id": row[0],
                "user_id": row[1],
                "conversation_id": row[2],
                "revision": row[3],
            }
        )
        graph = _current_for_mutation(
            conn,
            graph_id=str(item["id"]),
            user_id=str(item["user_id"]),
            conversation_id=str(item["conversation_id"]),
            expected_revision=int(item["revision"]),
        )
        retired.append(
            _publish_deterministic_retirement(
                conn,
                graph=graph,
                content=COMPARE_CURRENT_FILE_WEB_EXPIRED_RESPONSE,
                conversation_archived=False,
                cancelled=False,
                expired=True,
                now=timestamp,
            )
        )
    return tuple(retired)


def cancel_compare_current_file_web_work_graph_in_transaction(
    conn: sqlite3.Connection,
    *,
    graph_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    now: str | None = None,
) -> CompareCurrentFileWebWorkGraph:
    """Publish the one code-owned user cancellation and stop the active graph."""

    _require_transaction(conn)
    graph = _current_for_mutation(
        conn,
        graph_id=graph_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
    )
    timestamp = canonical_work_item_instant(
        now or datetime.now(UTC).isoformat(),
        label="now",
    )
    timestamp = max(timestamp, graph.updated_at)
    return _publish_deterministic_retirement(
        conn,
        graph=graph,
        content=COMPARE_CURRENT_FILE_WEB_CANCELLED_RESPONSE,
        conversation_archived=False,
        cancelled=True,
        expired=False,
        now=timestamp,
    )


def complete_compare_current_file_web_work_graph_in_transaction(
    conn: sqlite3.Connection,
    *,
    graph_id: str,
    user_id: str,
    conversation_id: str,
    expected_revision: int,
    publication_assistant_message_id: str,
    receipt: CompareCurrentFileWebPublicationReceipt,
    now: str | None = None,
) -> CompareCurrentFileWebWorkGraph:
    """Atomically bind one exact assistant receipt and complete the graph once."""

    _require_transaction(conn)
    graph = _current_for_mutation(
        conn,
        graph_id=graph_id,
        user_id=user_id,
        conversation_id=conversation_id,
        expected_revision=expected_revision,
    )
    if type(receipt) is not CompareCurrentFileWebPublicationReceipt:
        raise CompareCurrentFileWebGraphError("completion requires the exact publication receipt")
    expected_receipt = graph.publication_receipt(final_authority_rechecked=receipt.final_authority_rechecked)
    if receipt != expected_receipt:
        raise CompareCurrentFileWebGraphAnchorError("publication receipt does not match current graph state")
    timestamp = _logical_now(now, graph=graph)
    assistant = conn.execute(
        """SELECT metadata_json FROM messages
            WHERE id=? AND user_id=? AND conversation_id=?
              AND role='assistant' AND reply_to=?""",
        (
            publication_assistant_message_id,
            graph.user_id,
            graph.conversation_id,
            graph.anchor_user_message_id,
        ),
    ).fetchone()
    if assistant is None:
        raise CompareCurrentFileWebGraphAnchorError("publication assistant is outside the graph boundary")
    metadata_json = assistant["metadata_json"] if isinstance(assistant, sqlite3.Row) else assistant[0]
    load_compare_current_file_web_publication_receipt(str(metadata_json or "{}"), expected=receipt)
    receipt_sha256 = receipt.canonical_sha256()
    _begin_savepoint(conn)
    try:
        cursor = conn.execute(
            f"""UPDATE {_GRAPH_TABLE}
                   SET state='completed',revision=revision+1,transition='publication_committed',
                       updated_at=?,closed_at=?,outcome_status='complete',outcome_reason='none',
                       publication_assistant_message_id=?,
                       accepted_graph_outcome_sha256=?,accepted_steps_sha256=?,
                       publication_receipt_sha256=?
                 WHERE id=? AND user_id=? AND conversation_id=?
                   AND state='active' AND revision=? AND expires_at>?""",  # nosec B608
            (
                timestamp,
                timestamp,
                publication_assistant_message_id,
                receipt.graph_outcome_sha256,
                receipt.steps_sha256,
                receipt_sha256,
                graph.id,
                graph.user_id,
                graph.conversation_id,
                graph.revision,
                timestamp,
            ),
        )
        if cursor.rowcount != 1:
            raise CompareCurrentFileWebGraphConflictError("WorkGraph publication lost its CAS race")
        completed = _fetch(
            conn,
            graph_id=graph.id,
            user_id=graph.user_id,
            conversation_id=graph.conversation_id,
        )
        if completed is None:  # pragma: no cover
            raise CompareCurrentFileWebGraphConflictError("completed WorkGraph disappeared")
        _validate_stored_graph(conn, completed)
    except BaseException:
        _rollback_savepoint(conn)
        raise
    _release_savepoint(conn)
    return completed


__all__ = [
    "CompareCurrentFileWebGraphAnchorError",
    "CompareCurrentFileWebGraphConflictError",
    "admit_compare_current_file_web_review_recovery_in_transaction",
    "cancel_compare_current_file_web_work_graph_in_transaction",
    "claim_compare_current_file_web_step_in_transaction",
    "close_compare_current_file_web_work_graph_terminal_in_transaction",
    "complete_compare_current_file_web_work_graph_in_transaction",
    "create_compare_current_file_web_work_graph_in_transaction",
    "expire_due_compare_current_file_web_work_graphs_in_transaction",
    "get_compare_current_file_web_work_graph_in_transaction",
    "get_current_compare_current_file_web_work_graph_in_transaction",
    "prepare_compare_current_file_web_restart_rebind_in_transaction",
    "requeue_interrupted_compare_current_file_web_step_in_transaction",
    "retire_compare_current_file_web_work_graph_for_archived_conversation_in_transaction",
    "settle_compare_current_file_web_step_in_transaction",
]
