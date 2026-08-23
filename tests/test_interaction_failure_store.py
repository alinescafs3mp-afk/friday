"""P0B failures have one bounded private owner without borrowing runtime_events."""

from __future__ import annotations

import pytest

from friday.account_deletion import (
    _mark_account_deletion_history_clean,
    delete_account,
    preflight_account_deletion,
)
from friday.interaction_control_plane import FailureStage, PublicationStatus, TurnTrace, failure_store
from friday.interaction_control_plane.failure_store import (
    FailureEntrypoint,
    FailureRoute,
    FailureTraceScope,
    bind_failure_trace_scope,
    interaction_episode_baseline,
    record_precommit_failure,
)
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage._conversations import store_message_in_transaction


def _scope(user_id: str, conversation_id: str | None = None, *, ordinal: int = 1) -> FailureTraceScope:
    return FailureTraceScope(
        user_id=user_id,
        conversation_id=conversation_id,
        entrypoint=FailureEntrypoint.API_CHAT,
        route=FailureRoute.ARCHIVE_READ,
        stage=FailureStage.CAPABILITY,
        turn_identifier=f"synthetic-failure-turn-{ordinal}",
    )


def test_a_precommit_failure_is_hmac_scoped_body_free_and_not_a_runtime_event(storage) -> None:
    user_id = "failure-owner-alice"
    storage.ensure_user(user_id)
    conversation = storage.create_conversation(user_id, "private title must not enter trace")
    before_events = storage.count_events()
    scope = _scope(user_id, conversation["id"])

    assert record_precommit_failure(storage, scope, RuntimeError("PRIVATE ERROR BODY 8142")) is True
    row = storage.execute("SELECT * FROM interaction_failure_traces").fetchone()
    assert row is not None
    trace = TurnTrace.parse(row["trace_json"])
    assert trace.publication is PublicationStatus.NOT_ATTEMPTED
    assert trace.failure_stage is FailureStage.CAPABILITY
    assert row["user_id"] == user_id
    assert row["conversation_id"] == conversation["id"]
    assert user_id not in row["trace_json"]
    assert conversation["id"] not in row["trace_json"]
    assert "PRIVATE ERROR BODY" not in row["trace_json"]
    assert storage.count_events() == before_events


def test_a_durable_assistant_candidate_prevents_a_false_precommit_failure(storage) -> None:
    user_id = "failure-commit-owner"
    storage.ensure_user(user_id)
    conversation = storage.create_conversation(user_id, "commit")
    scope = _scope(user_id, conversation["id"])
    with bind_failure_trace_scope(scope):
        storage.store_message(conversation["id"], user_id, "assistant", "already committed")

    assert record_precommit_failure(storage, scope, RuntimeError("after commit")) is False
    assert storage.execute("SELECT COUNT(*) FROM interaction_failure_traces").fetchone()[0] == 0


def test_new_conversation_and_user_message_become_hmac_scoped_failure_owners(storage) -> None:
    user_id = "failure-new-conversation-owner"
    storage.ensure_user(user_id)
    scope = _scope(user_id)
    with bind_failure_trace_scope(scope):
        conversation = storage.create_conversation(user_id, "private new title")
        user_message = storage.store_message(
            conversation["id"],
            user_id,
            "user",
            "PRIVATE REQUEST BODY 5291",
        )

    assert record_precommit_failure(storage, scope, RuntimeError("PRIVATE ERROR BODY 5291"))
    row = storage.execute("SELECT * FROM interaction_failure_traces").fetchone()
    assert row["conversation_id"] == conversation["id"]
    assert conversation["id"] not in row["trace_json"]
    assert user_message["id"] not in row["trace_json"]
    assert "PRIVATE REQUEST BODY" not in row["trace_json"]


def test_a_broken_exception_projection_cannot_replace_the_original_failure(storage) -> None:
    class HostileFailure(RuntimeError):
        @property
        def status_code(self):
            raise RuntimeError("hostile status projection")

    user_id = "failure-hostile-exception-owner"
    storage.ensure_user(user_id)
    assert record_precommit_failure(storage, _scope(user_id), HostileFailure()) is False
    assert storage.execute("SELECT COUNT(*) FROM interaction_failure_traces").fetchone()[0] == 0


def test_a_rolled_back_assistant_candidate_does_not_hide_the_failure(storage) -> None:
    user_id = "failure-rollback-owner"
    storage.ensure_user(user_id)
    conversation = storage.create_conversation(user_id, "rollback")
    scope = _scope(user_id, conversation["id"])
    with (
        pytest.raises(RuntimeError, match="rollback"),
        bind_failure_trace_scope(scope),
        storage.transaction() as conn,
    ):
        store_message_in_transaction(
            conn,
            conversation["id"],
            user_id,
            "assistant",
            "must roll back",
        )
        raise RuntimeError("rollback")

    assert record_precommit_failure(storage, scope, RuntimeError("private failure")) is True
    assert storage.execute("SELECT COUNT(*) FROM messages WHERE role='assistant'").fetchone()[0] == 0


def test_conversation_removal_erases_its_failure_traces_but_keeps_chat(storage) -> None:
    user_id = "failure-conversation-owner"
    storage.ensure_user(user_id)
    conversation = storage.create_conversation(user_id, "remove")
    storage.store_message(conversation["id"], user_id, "user", "keep chat")
    assert record_precommit_failure(storage, _scope(user_id, conversation["id"]), RuntimeError())

    result = storage.delete_conversation(conversation["id"], user_id)
    assert result["messages_kept"] == 1
    assert result["deleted"]["interaction_failure_traces"] == 1
    assert storage.execute("SELECT COUNT(*) FROM interaction_failure_traces").fetchone()[0] == 0


def test_account_deletion_counts_and_erases_failure_traces(storage) -> None:
    target = "local:failure-delete-target"
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    storage.ensure_user(target, source="local")
    assert _mark_account_deletion_history_clean(storage, target) is True
    assert record_precommit_failure(storage, _scope(target), RuntimeError())
    storage.update_user(target, status="disabled")
    plan = preflight_account_deletion(storage, target, quiescence_available=True)
    assert plan["ready"] is True
    assert plan["counts"]["interaction_failure_traces"] == 1

    result = delete_account(
        storage,
        target,
        expected_fingerprint=plan["fingerprint"],
        actor_user_id=LEGACY_OWNER_USER_ID,
        quiescence_verified=True,
    )
    assert result["deleted"]["interaction_failure_traces"] == 1
    assert storage.get_user(target) is None


def test_retention_is_bounded_and_episode_report_contains_closed_signals(storage, monkeypatch) -> None:
    user_id = "failure-retention-owner"
    storage.ensure_user(user_id)
    monkeypatch.setattr(failure_store, "INTERACTION_FAILURE_PER_USER_CAP", 3)
    for ordinal in range(1, 6):
        assert record_precommit_failure(storage, _scope(user_id, ordinal=ordinal), TimeoutError())

    assert storage.execute(
        "SELECT COUNT(*) FROM interaction_failure_traces WHERE user_id=?", (user_id,)
    ).fetchone()[0] == 3
    report = interaction_episode_baseline(storage, user_id)
    assert report["precommit_failures"] == 3
    assert report["assistant_committed"] == 0
    assert report["publication"] == {"not_attempted": 3}
    assert report["failure_reasons"] == {"timeout": 3}
    assert report["precommit_routes"] == {"archive_read": 3}
    assert report["signals"] == {
        "ambiguity_present": 0,
        "partial_coverage": 0,
        "state_restored": 0,
        "authority_rechecked": 0,
    }
