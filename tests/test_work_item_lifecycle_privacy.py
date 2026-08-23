"""Work Item account, export and conversation-lifecycle privacy boundaries."""

from __future__ import annotations

import json
from pathlib import Path

from friday.account_deletion import (
    _mark_account_deletion_history_clean,
    preflight_account_deletion,
)
from friday.interaction_control_plane.work_item_contract import RecallConversationActiveFrame
from friday.orchestration.capability_outcome import (
    CapabilityOutcome,
    CapabilityOutcomeStatus,
    attach_accepted_capability_outcome_receipt,
)
from friday.orchestration.contracts import RouteClass
from friday.orchestration.message_window_outcome import LegacyMessageWindowPlan
from friday.storage.models import new_id

_NOW = "2026-08-23T08:00:00+00:00"
_EXPIRES = "2026-08-23T20:00:00+00:00"


def _seed_work_item(storage, user_id: str, *, revision: int = 1) -> dict[str, str]:
    storage.ensure_user(user_id, source="local")
    conversation = storage.create_conversation(user_id, "Private recall")
    request = "Что я писал вчера?"
    user_message = storage.store_message(
        conversation["id"],
        user_id,
        "user",
        request,
    )
    frame = RecallConversationActiveFrame.create(
        timezone_name="Europe/Moscow",
        since_utc="2026-08-21T21:00:00+00:00",
        until_utc="2026-08-22T21:00:00+00:00",
    )
    plan = LegacyMessageWindowPlan.from_request(
        request,
        tenant_id=user_id,
        person_id=user_id,
        conversation_id=conversation["id"],
        timezone_name=frame.timezone_name,
        since_utc=frame.since_utc,
        until_utc=frame.until_utc,
        boundary_message_id=user_message["id"],
    )
    plan_sha256 = plan.canonical_sha256()
    outcome = CapabilityOutcome(
        route=RouteClass.ORDINARY_DIALOGUE,
        status=CapabilityOutcomeStatus.EMPTY,
        plan_sha256=plan_sha256,
        evidence_identity_sha256="c" * 64,
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
    assistant_message = storage.store_message(
        conversation["id"],
        user_id,
        "assistant",
        "Принятый ответ",
        metadata=metadata,
        reply_to=user_message["id"],
    )
    work_item_id = new_id("work")
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO work_items(
                   id,user_id,conversation_id,kind,goal,state,playbook,
                   completion_contract,active_frame_json,anchor_user_message_id,
                   anchor_assistant_message_id,accepted_plan_sha256,
                   accepted_outcome_sha256,revision,transition,created_at,
                   updated_at,expires_at,closed_at
               ) VALUES(?,?,?,'recall_conversation','exact_current_conversation_recall',
                        'active','recall_conversation','accepted_exact_owned_message_window',
                        ?,?,?,?, ?,?,?, ?,?,?,NULL)""",
            (
                work_item_id,
                user_id,
                conversation["id"],
                frame.to_json(),
                user_message["id"],
                assistant_message["id"],
                plan_sha256,
                receipt.outcome_sha256,
                revision,
                "created" if revision == 1 else "constraint_updated",
                _NOW,
                _NOW,
                _EXPIRES,
            ),
        )
    return {
        "id": work_item_id,
        "conversation_id": conversation["id"],
        "user_message_id": user_message["id"],
        "assistant_message_id": assistant_message["id"],
    }


def test_conversation_removal_cancels_open_work_without_erasing_chat(storage) -> None:
    work = _seed_work_item(storage, "alice")

    report = storage.delete_conversation(work["conversation_id"], "alice")

    assert report["messages_kept"] == 2
    assert report["cancelled"] == {"work_items": 1}
    assert "work_items" not in report["deleted"]
    row = storage.execute("SELECT * FROM work_items WHERE id=?", (work["id"],)).fetchone()
    assert row is not None
    assert (row["state"], row["transition"], row["revision"]) == (
        "cancelled",
        "cancelled",
        2,
    )
    assert row["closed_at"] == row["updated_at"]
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=?",
            (work["conversation_id"],),
        ).fetchone()[0]
        == 2
    )


def test_conversation_removal_erases_revision_exhausted_control_state(storage) -> None:
    work = _seed_work_item(storage, "alice-max-revision", revision=2_147_483_647)

    report = storage.delete_conversation(work["conversation_id"], "alice-max-revision")

    assert report["cancelled"] == {"work_items": 0}
    assert report["deleted"]["work_items"] == 1
    assert storage.execute("SELECT 1 FROM work_items WHERE id=?", (work["id"],)).fetchone() is None
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=?",
            (work["conversation_id"],),
        ).fetchone()[0]
        == 2
    )


def test_user_export_projects_only_valid_owned_work_items(storage) -> None:
    alice = _seed_work_item(storage, "alice-export")
    bob = _seed_work_item(storage, "bob-export")

    exported = storage.export_user("alice-export")
    payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))

    assert len(payload["work_items"]) == 1
    item = payload["work_items"][0]
    assert item["schema"] == "friday.recall-conversation-work-item.v1"
    assert item["id"] == alice["id"]
    assert item["user_id"] == "alice-export"
    assert item["active_frame"] == {
        "schema": "friday.recall-conversation-active-frame.v1",
        "source_scope": "current_conversation",
        "timezone_name": "Europe/Moscow",
        "since_utc": "2026-08-21T21:00:00+00:00",
        "until_utc": "2026-08-22T21:00:00+00:00",
        "role": "any",
    }
    assert bob["id"] not in Path(exported["path"]).read_text(encoding="utf-8")


def test_user_export_integrity_checks_and_keeps_a_disabled_owners_valid_work(storage) -> None:
    work = _seed_work_item(storage, "disabled-export-owner")
    storage.update_user("disabled-export-owner", status="disabled")

    exported = storage.export_user("disabled-export-owner")
    payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))

    assert [item["id"] for item in payload["work_items"]] == [work["id"]]


def test_user_export_omits_a_work_item_with_a_foreign_message_anchor(storage) -> None:
    alice = _seed_work_item(storage, "alice-foreign-anchor")
    bob = _seed_work_item(storage, "bob-foreign-anchor")
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE work_items SET anchor_assistant_message_id=? WHERE id=?",
            (bob["assistant_message_id"], alice["id"]),
        )

    exported = storage.export_user("alice-foreign-anchor")
    payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))

    assert payload["work_items"] == []


def test_user_export_omits_a_work_item_without_its_accepted_receipt(storage) -> None:
    work = _seed_work_item(storage, "alice-missing-receipt")
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE messages SET metadata_json='{}' WHERE id=?",
            (work["assistant_message_id"],),
        )

    exported = storage.export_user("alice-missing-receipt")
    payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))

    assert payload["work_items"] == []


def test_account_deletion_inventory_counts_work_items_and_classifies_their_scope(storage) -> None:
    target = "local:work-item-delete-target"
    _seed_work_item(storage, target)
    assert _mark_account_deletion_history_clean(storage, target)
    storage.update_user(target, status="disabled")

    plan = preflight_account_deletion(storage, target, quiescence_available=True)

    assert plan["counts"]["work_items"] == 1
    assert plan["unknown_scopes"] == []
    assert {item["code"] for item in plan["blockers"]} == {"chat_history"}


def test_foreign_work_frame_is_a_fail_closed_account_deletion_blocker(storage) -> None:
    target = "local:work-frame-private-target"
    storage.ensure_user(target, source="local")
    assert _mark_account_deletion_history_clean(storage, target)
    storage.update_user(target, status="disabled")
    foreign = _seed_work_item(storage, "foreign-frame-owner")
    hostile_frame = {
        "schema": "friday.recall-conversation-active-frame.v1",
        "source_scope": "current_conversation",
        "timezone_name": target,
        "since_utc": "2026-08-21T21:00:00+00:00",
        "until_utc": "2026-08-22T21:00:00+00:00",
        "role": "any",
    }
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE work_items SET active_frame_json=? WHERE id=?",
            (json.dumps(hostile_frame, sort_keys=True, separators=(",", ":")), foreign["id"]),
        )

    plan = preflight_account_deletion(storage, target, quiescence_available=True)

    assert plan["cross_account_json_references"] == {"work_items.active_frame_json": 1}
    assert {item["code"] for item in plan["blockers"]} == {"cross_account_json_references"}
