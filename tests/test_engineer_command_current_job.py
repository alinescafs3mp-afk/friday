"""Durable, exact-scope current-job selection for Engineer commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from friday.organs.engineer.command import CommandError
from friday.organs.engineer.command.store import CommandJobStore

ACTOR = "owner-1"
TENANT = "tenant-1"
CONVERSATION = "conv-1"
CHANNEL = "cli_test"


def _payload(
    job_id: str,
    *,
    status: str,
    actor_id: str = ACTOR,
    tenant_id: str = TENANT,
    conversation_id: str = CONVERSATION,
    channel: str = CHANNEL,
    key: str | None = None,
) -> dict[str, object]:
    return {
        "job_id": job_id,
        "actor_id": actor_id,
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "channel": channel,
        "source_row_id": f"row-{job_id[:4]}",
        "source_hash": "1" * 64,
        "telegram_update_id": f"upd-{job_id[:4]}",
        "isolation_profile": "isolated_workspace",
        "host_user_authorized": False,
        "idempotency_key": key or f"idem-{job_id}",
        "command_digest": "2" * 64,
        "argv_sha256": "3" * 64,
        "lane": "argv",
        "origin": "owner_turn",
        "status": status,
        "error_code": "",
        "grant_nonce": f"nonce-{job_id}",
        "timeout_sec": 30,
        "max_stdout_bytes": 1024,
        "max_stderr_bytes": 1024,
        "created_at": 100.0,
        "executable_json": None,
    }


def _insert(store: CommandJobStore, payload: dict[str, object]) -> None:
    with store.transaction():
        store.insert_job(payload)


def _resolve(
    store: CommandJobStore,
    job_id: str | None = None,
    *,
    operation: str = "status",
    actor_id: str = ACTOR,
    tenant_id: str = TENANT,
    conversation_id: str = CONVERSATION,
    channel: str = CHANNEL,
) -> str:
    return store.resolve_job_reference(
        job_id,
        actor_id=actor_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
        operation=operation,  # type: ignore[arg-type]
        requested_at=200.0,
    )


def test_current_reference_is_exact_scope_and_never_crosses_conversations(tmp_path: Path) -> None:
    store = CommandJobStore(tmp_path / "store")
    try:
        first = "1" * 32
        second = "2" * 32
        _insert(store, _payload(first, status="completed"))
        _insert(store, _payload(second, status="completed", conversation_id="conv-2"))

        assert _resolve(store) == first
        assert _resolve(store, conversation_id="conv-2") == second
        with pytest.raises(CommandError, match="job_scope_mismatch"):
            _resolve(store, second)
    finally:
        store.close()


def test_two_unresolved_jobs_are_ambiguous_even_with_a_durable_focus(tmp_path: Path) -> None:
    store = CommandJobStore(tmp_path / "store")
    try:
        _insert(store, _payload("1" * 32, status="running"))
        _insert(store, _payload("2" * 32, status="admitted"))

        with pytest.raises(CommandError, match="current_job_ambiguous"):
            _resolve(store)
        with pytest.raises(CommandError, match="current_job_ambiguous"):
            _resolve(store, operation="cancel")
        rows = store._conn.execute(  # noqa: SLF001 - exact durable-state assertion
            "SELECT cancel_requested_at FROM jobs ORDER BY job_id"
        ).fetchall()
        assert [row[0] for row in rows] == [None, None]
    finally:
        store.close()


def test_no_current_job_and_legacy_multiple_terminal_jobs_fail_closed(tmp_path: Path) -> None:
    store = CommandJobStore(tmp_path / "store")
    try:
        with pytest.raises(CommandError, match="current_job_not_found"):
            _resolve(store)
        _insert(store, _payload("1" * 32, status="completed"))
        _insert(store, _payload("2" * 32, status="failed"))
        with store.transaction() as conn:
            conn.execute("DELETE FROM command_job_focus")

        with pytest.raises(CommandError, match="current_job_ambiguous"):
            _resolve(store)
    finally:
        store.close()


def test_unknown_job_is_status_readable_but_never_cancelled(tmp_path: Path) -> None:
    store = CommandJobStore(tmp_path / "store")
    try:
        job_id = "1" * 32
        _insert(store, _payload(job_id, status="unknown"))
        assert _resolve(store) == job_id

        with pytest.raises(CommandError, match="current_job_uncertain"):
            _resolve(store, operation="cancel")
        assert store.read_job(job_id)["cancel_requested_at"] is None
    finally:
        store.close()


def test_cancel_selection_persists_one_idempotent_intent_before_return(tmp_path: Path) -> None:
    store = CommandJobStore(tmp_path / "store")
    try:
        job_id = "1" * 32
        _insert(store, _payload(job_id, status="running"))

        assert _resolve(store, operation="cancel") == job_id
        assert store.read_job(job_id)["cancel_requested_at"] == 200.0
        assert store.resolve_job_reference(
            job_id,
            actor_id=ACTOR,
            tenant_id=TENANT,
            conversation_id=CONVERSATION,
            channel=CHANNEL,
            operation="cancel",
            requested_at=300.0,
        ) == job_id
        assert store.read_job(job_id)["cancel_requested_at"] == 200.0
    finally:
        store.close()


def test_explicit_reference_refocuses_but_lookup_replay_does_not(tmp_path: Path) -> None:
    store = CommandJobStore(tmp_path / "store")
    try:
        first = "1" * 32
        second = "2" * 32
        _insert(store, _payload(first, status="completed", key="idem-first"))
        _insert(store, _payload(second, status="completed", key="idem-second"))
        assert _resolve(store) == second

        # The idempotency lookup used by submit is deliberately observational.
        assert store.locked_lookup_idempotency(ACTOR, "idem-first") == {
            "job_id": first,
            "digest": "2" * 64,
        }
        assert _resolve(store) == second

        assert _resolve(store, first) == first
        assert _resolve(store) == first
    finally:
        store.close()
