"""Direct adversarial coverage for the schema-38 Work Item store."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from friday.interaction_control_plane.engineer_work_item import (
    EngineerWorkItemChannel,
    create_engineer_work_item_in_transaction,
)
from friday.interaction_control_plane.engineer_work_item_schema import (
    ENGINEER_WORK_ITEM_COMPLETION_CONTRACT_SHA256,
)
from friday.interaction_control_plane.work_item_contract import (
    RecallConversationActiveFrame,
    RecallConversationWorkItem,
    RecallMessageRole,
    WorkItemContractError,
    WorkTransition,
)
from friday.interaction_control_plane.work_item_store import (
    WorkItemAnchorError,
    WorkItemConflictError,
    cancel_recall_conversation_work_item_in_transaction,
    cas_update_recall_conversation_constraints_in_transaction,
    create_recall_conversation_work_item_in_transaction,
    get_recall_conversation_work_item_for_export_in_transaction,
    get_recall_conversation_work_item_in_transaction,
)
from friday.orchestration.capability_outcome import (
    CapabilityOutcome,
    CapabilityOutcomeStatus,
    attach_accepted_capability_outcome_receipt,
)
from friday.orchestration.contracts import RouteClass
from friday.orchestration.message_window_outcome import LegacyMessageWindowPlan

_NOW = "2026-08-23T08:00:00+00:00"


def _frame() -> RecallConversationActiveFrame:
    return RecallConversationActiveFrame.create(
        timezone_name="Europe/Moscow",
        since_utc="2026-08-21T21:00:00+00:00",
        until_utc="2026-08-22T21:00:00+00:00",
    )


def _accepted_assistant(
    storage: Any,
    *,
    user_id: str,
    conversation_id: str,
    boundary: dict[str, Any],
    frame: RecallConversationActiveFrame,
) -> tuple[dict[str, Any], LegacyMessageWindowPlan, str]:
    plan = LegacyMessageWindowPlan.from_request(
        str(boundary["content"]),
        tenant_id=f"tenant:{user_id}",
        person_id=user_id,
        conversation_id=conversation_id,
        timezone_name=frame.timezone_name,
        since_utc=frame.since_utc,
        until_utc=frame.until_utc,
        boundary_message_id=str(boundary["id"]),
    )
    outcome = CapabilityOutcome(
        route=RouteClass.ORDINARY_DIALOGUE,
        status=CapabilityOutcomeStatus.EMPTY,
        plan_sha256=plan.canonical_sha256(),
        evidence_identity_sha256="e" * 64,
        citation_labels=(),
        authority_rechecked=True,
        verified=True,
    )
    metadata: dict[str, object] = {
        "structural": {
            "verdict_kind": "message_window",
            "answer_present": True,
            "model_spoke": False,
            "message_window_status": outcome.status.value,
            "accepted_message_window_plan": plan.payload(),
        }
    }
    receipt = attach_accepted_capability_outcome_receipt(metadata, outcome)
    assistant = storage.store_message(
        conversation_id,
        user_id,
        "assistant",
        "Принятый структурный ответ",
        metadata=metadata,
        reply_to=str(boundary["id"]),
    )
    return assistant, plan, receipt.outcome_sha256


def _create_work(storage: Any, user_id: str) -> tuple[RecallConversationWorkItem, dict[str, Any]]:
    storage.ensure_user(user_id, source="local")
    conversation = storage.create_conversation(user_id, "Work Item integrity")
    request = "Что я писал вчера?"
    boundary = storage.store_message(str(conversation["id"]), user_id, "user", request)
    frame = _frame()
    assistant, plan, outcome_sha256 = _accepted_assistant(
        storage,
        user_id=user_id,
        conversation_id=str(conversation["id"]),
        boundary=boundary,
        frame=frame,
    )
    with storage.transaction() as conn:
        item = create_recall_conversation_work_item_in_transaction(
            conn,
            user_id=user_id,
            conversation_id=str(conversation["id"]),
            timezone_name=frame.timezone_name,
            since_utc=frame.since_utc,
            until_utc=frame.until_utc,
            role=RecallMessageRole.ANY,
            anchor_user_message_id=str(boundary["id"]),
            anchor_assistant_message_id=str(assistant["id"]),
            accepted_plan_sha256=plan.canonical_sha256(),
            accepted_outcome_sha256=outcome_sha256,
            now=_NOW,
        )
    return item, assistant


def test_engineer_work_item_reciprocally_blocks_ordinary_open_work(storage: Any) -> None:
    user_id = "engineer-exclusive-owner"
    storage.ensure_user(user_id, source="local")
    conversation = storage.create_conversation(user_id, "Engineer exclusivity")
    boundary = storage.store_message(
        str(conversation["id"]),
        user_id,
        "user",
        "Выполни многошаговую инженерную задачу",
    )
    frame = _frame()
    assistant, plan, outcome_sha256 = _accepted_assistant(
        storage,
        user_id=user_id,
        conversation_id=str(conversation["id"]),
        boundary=boundary,
        frame=frame,
    )
    with storage.transaction() as conn:
        create_engineer_work_item_in_transaction(
            conn,
            owner_id=user_id,
            tenant_id=user_id,
            conversation_id=str(conversation["id"]),
            channel=EngineerWorkItemChannel.TELEGRAM,
            source_binding_sha256="a" * 64,
            completion_contract_sha256=ENGINEER_WORK_ITEM_COMPLETION_CONTRACT_SHA256,
            idempotency_key="ecmd-" + "b" * 64,
            command_digest="c" * 64,
            now=_NOW,
            expires_at="2026-08-23T20:00:00+00:00",
        )

    with pytest.raises(WorkItemConflictError), storage.transaction() as conn:
        create_recall_conversation_work_item_in_transaction(
            conn,
            user_id=user_id,
            conversation_id=str(conversation["id"]),
            timezone_name=frame.timezone_name,
            since_utc=frame.since_utc,
            until_utc=frame.until_utc,
            role=RecallMessageRole.ANY,
            anchor_user_message_id=str(boundary["id"]),
            anchor_assistant_message_id=str(assistant["id"]),
            accepted_plan_sha256=plan.canonical_sha256(),
            accepted_outcome_sha256=outcome_sha256,
            now=_NOW,
        )


def test_create_requires_exact_boundary_to_assistant_adjacency(storage: Any) -> None:
    user_id = "work-adjacent-owner"
    storage.ensure_user(user_id, source="local")
    conversation = storage.create_conversation(user_id, "adjacency")
    boundary = storage.store_message(str(conversation["id"]), user_id, "user", "Вчера?")
    storage.store_message(str(conversation["id"]), user_id, "assistant", "INTERVENING")
    frame = _frame()
    assistant, plan, outcome_sha256 = _accepted_assistant(
        storage,
        user_id=user_id,
        conversation_id=str(conversation["id"]),
        boundary=boundary,
        frame=frame,
    )

    with pytest.raises(WorkItemAnchorError, match="owned and exact"), storage.transaction() as conn:
        create_recall_conversation_work_item_in_transaction(
            conn,
            user_id=user_id,
            conversation_id=str(conversation["id"]),
            timezone_name=frame.timezone_name,
            since_utc=frame.since_utc,
            until_utc=frame.until_utc,
            role=RecallMessageRole.ANY,
            anchor_user_message_id=str(boundary["id"]),
            anchor_assistant_message_id=str(assistant["id"]),
            accepted_plan_sha256=plan.canonical_sha256(),
            accepted_outcome_sha256=outcome_sha256,
            now=_NOW,
        )


def test_create_rechecks_that_the_assistant_is_the_final_publication(storage: Any) -> None:
    user_id = "work-final-owner"
    storage.ensure_user(user_id, source="local")
    conversation = storage.create_conversation(user_id, "final publication")
    boundary = storage.store_message(str(conversation["id"]), user_id, "user", "Вчера?")
    frame = _frame()
    assistant, plan, outcome_sha256 = _accepted_assistant(
        storage,
        user_id=user_id,
        conversation_id=str(conversation["id"]),
        boundary=boundary,
        frame=frame,
    )
    storage.store_message(str(conversation["id"]), user_id, "user", "LATER")

    with pytest.raises(WorkItemAnchorError, match="latest"), storage.transaction() as conn:
        create_recall_conversation_work_item_in_transaction(
            conn,
            user_id=user_id,
            conversation_id=str(conversation["id"]),
            timezone_name=frame.timezone_name,
            since_utc=frame.since_utc,
            until_utc=frame.until_utc,
            role=RecallMessageRole.ANY,
            anchor_user_message_id=str(boundary["id"]),
            anchor_assistant_message_id=str(assistant["id"]),
            accepted_plan_sha256=plan.canonical_sha256(),
            accepted_outcome_sha256=outcome_sha256,
            now=_NOW,
        )


def test_final_store_recheck_closes_a_same_transaction_publication_race(storage: Any) -> None:
    user_id = "work-final-recheck-owner"
    storage.ensure_user(user_id, source="local")
    conversation = storage.create_conversation(user_id, "final recheck")
    boundary = storage.store_message(str(conversation["id"]), user_id, "user", "Вчера?")
    frame = _frame()
    assistant, plan, outcome_sha256 = _accepted_assistant(
        storage,
        user_id=user_id,
        conversation_id=str(conversation["id"]),
        boundary=boundary,
        frame=frame,
    )

    with pytest.raises(WorkItemAnchorError, match="latest"), storage.transaction() as conn:
        conn.execute(
            """CREATE TEMP TRIGGER work_item_late_message
               AFTER INSERT ON work_items BEGIN
                 INSERT INTO messages(
                     id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
                 ) VALUES(
                     'msg_eeeeeeeeeeeeeeee',NEW.conversation_id,NEW.user_id,'user',
                     'LATE-IN-TRANSACTION','{}',NULL,'2026-08-23T08:00:01+00:00'
                 );
               END"""
        )
        create_recall_conversation_work_item_in_transaction(
            conn,
            user_id=user_id,
            conversation_id=str(conversation["id"]),
            timezone_name=frame.timezone_name,
            since_utc=frame.since_utc,
            until_utc=frame.until_utc,
            role=RecallMessageRole.ANY,
            anchor_user_message_id=str(boundary["id"]),
            anchor_assistant_message_id=str(assistant["id"]),
            accepted_plan_sha256=plan.canonical_sha256(),
            accepted_outcome_sha256=outcome_sha256,
            now=_NOW,
        )

    assert storage.execute("SELECT 1 FROM work_items WHERE user_id=?", (user_id,)).fetchone() is None
    assert storage.execute("SELECT 1 FROM messages WHERE id='msg_eeeeeeeeeeeeeeee'").fetchone() is None


def test_final_cas_recheck_closes_a_same_transaction_publication_race(storage: Any) -> None:
    item, _assistant = _create_work(storage, "work-cas-recheck-owner")
    boundary = storage.store_message(item.conversation_id, item.user_id, "user", "А позавчера?")
    frame = item.active_frame.with_time_window(
        since_utc="2026-08-20T21:00:00+00:00",
        until_utc="2026-08-21T21:00:00+00:00",
    )
    assistant, plan, outcome_sha256 = _accepted_assistant(
        storage,
        user_id=item.user_id,
        conversation_id=item.conversation_id,
        boundary=boundary,
        frame=frame,
    )

    with pytest.raises(WorkItemAnchorError, match="latest"), storage.transaction() as conn:
        conn.execute(
            """CREATE TEMP TRIGGER work_item_cas_late_message
               AFTER UPDATE OF anchor_assistant_message_id ON work_items BEGIN
                 INSERT INTO messages(
                     id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
                 ) VALUES(
                     'msg_dddddddddddddddd',NEW.conversation_id,NEW.user_id,'user',
                     'LATE-CAS-IN-TRANSACTION','{}',NULL,'2026-08-23T08:00:01+00:00'
                 );
               END"""
        )
        cas_update_recall_conversation_constraints_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=item.revision,
            since_utc=frame.since_utc,
            until_utc=frame.until_utc,
            new_boundary_user_message_id=str(boundary["id"]),
            new_assistant_message_id=str(assistant["id"]),
            new_accepted_plan_sha256=plan.canonical_sha256(),
            new_accepted_outcome_sha256=outcome_sha256,
            now="2026-08-23T08:00:01+00:00",
        )

    row = storage.execute("SELECT * FROM work_items WHERE id=?", (item.id,)).fetchone()
    assert row is not None
    assert RecallConversationWorkItem.from_storage_row(dict(row)) == item
    assert storage.execute("SELECT 1 FROM messages WHERE id='msg_dddddddddddddddd'").fetchone() is None


def test_getter_rejects_an_active_frame_detached_from_the_accepted_plan(storage: Any) -> None:
    item, _assistant = _create_work(storage, "work-frame-tamper-owner")
    tampered = item.active_frame.with_time_window(
        since_utc="2026-08-20T21:00:00+00:00",
        until_utc="2026-08-21T21:00:00+00:00",
    )
    storage.execute("UPDATE work_items SET active_frame_json=? WHERE id=?", (tampered.to_json(), item.id))
    storage.conn.commit()

    with storage.transaction() as conn, pytest.raises(WorkItemAnchorError, match="active frame"):
        get_recall_conversation_work_item_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
        )


def test_getter_rejects_a_tampered_digest_only_plan_carrier(storage: Any) -> None:
    item, assistant = _create_work(storage, "work-plan-tamper-owner")
    metadata = json.loads(str(assistant["metadata_json"]))
    metadata["structural"]["accepted_message_window_plan"]["since_utc_sha256"] = "f" * 64
    storage.execute(
        "UPDATE messages SET metadata_json=? WHERE id=?",
        (json.dumps(metadata, sort_keys=True), str(assistant["id"])),
    )
    storage.conn.commit()

    with storage.transaction() as conn, pytest.raises(WorkItemAnchorError, match="active frame"):
        get_recall_conversation_work_item_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
        )


def test_disabled_owner_is_exportable_but_not_runtime_current_authority(storage: Any) -> None:
    item, _assistant = _create_work(storage, "work-disabled-owner")
    storage.update_user(item.user_id, status="disabled")

    with storage.transaction() as conn:
        with pytest.raises(WorkItemAnchorError, match="owned and exact"):
            get_recall_conversation_work_item_in_transaction(
                conn,
                work_item_id=item.id,
                user_id=item.user_id,
                conversation_id=item.conversation_id,
            )
        exported = get_recall_conversation_work_item_for_export_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
        )

    assert exported == item


def test_python_and_ddl_reject_ttl_beyond_twelve_hours(storage: Any) -> None:
    item, _assistant = _create_work(storage, "work-ttl-owner")
    too_late = (datetime.fromisoformat(item.updated_at) + timedelta(hours=12, seconds=1)).isoformat()

    with pytest.raises(WorkItemContractError, match="TTL"):
        replace(item, expires_at=too_late)
    with pytest.raises(sqlite3.IntegrityError), storage.transaction() as conn:
        conn.execute("UPDATE work_items SET expires_at=? WHERE id=?", (too_late, item.id))


def test_python_and_ddl_reject_revision_one_post_create_transition(storage: Any) -> None:
    item, _assistant = _create_work(storage, "work-revision-owner")

    with pytest.raises(WorkItemContractError, match="revision 2"):
        replace(item, transition=WorkTransition.CONSTRAINT_UPDATED)
    with pytest.raises(sqlite3.IntegrityError), storage.transaction() as conn:
        conn.execute(
            "UPDATE work_items SET transition='constraint_updated' WHERE id=?",
            (item.id,),
        )


def test_python_and_ddl_require_terminal_closed_at_to_equal_updated_at(storage: Any) -> None:
    item, _assistant = _create_work(storage, "work-closed-owner")
    terminal_at = (datetime.fromisoformat(item.updated_at) + timedelta(seconds=1)).isoformat()
    with storage.transaction() as conn:
        cancelled = cancel_recall_conversation_work_item_in_transaction(
            conn,
            work_item_id=item.id,
            user_id=item.user_id,
            conversation_id=item.conversation_id,
            expected_revision=item.revision,
            now=terminal_at,
        )
    wrong_closed_at = (datetime.fromisoformat(terminal_at) + timedelta(seconds=1)).isoformat()

    with pytest.raises(WorkItemContractError, match="terminal update"):
        replace(cancelled, closed_at=wrong_closed_at)
    with pytest.raises(sqlite3.IntegrityError), storage.transaction() as conn:
        conn.execute("UPDATE work_items SET closed_at=? WHERE id=?", (wrong_closed_at, item.id))
