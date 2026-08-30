"""Runtime activation for the bounded conversation-passage writer."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from typing import Any

import pytest

import friday.storage._conversation_passages as conversation_storage_module
import friday.workers as workers_module
from friday.conversation_passages.worker_state import (
    CONVERSATION_PASSAGE_MAX_GENERATION,
    CONVERSATION_PASSAGE_WORKER_STATE_KEY,
    ConversationPassageWorkerState,
    decode_conversation_passage_worker_state,
    encode_conversation_passage_worker_state,
)
from friday.conversation_passages.writer import (
    CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES,
)
from friday.storage import FridayStorage
from friday.storage._base import utc_now
from friday.workers import WorkerBatchError, WorkersManager


def _conversation(storage: Any, owner: str, body: str) -> str:
    conversation_id = str(storage.create_conversation(owner)["id"])
    storage.store_message(conversation_id, owner, "user", body)
    return conversation_id


def _projection_status(storage: Any, conversation_id: str) -> str:
    row = storage.execute(
        "SELECT projection_status FROM conversation_passage_projections WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _owner_rowid(storage: Any, owner: str) -> int:
    row = storage.execute("SELECT rowid FROM users WHERE id=?", (owner,)).fetchone()
    assert row is not None and type(row[0]) is int
    return int(row[0])


def _empty_report() -> dict[str, object]:
    return {
        "examined": 0,
        "anchors_written": 0,
        "current": 0,
        "explicit_incomplete": 0,
        "has_more": False,
        "next_resume_conversation_id": None,
        "message_bytes_examined": 0,
        "message_byte_budget": CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES,
        "byte_budget_reached": False,
    }


def _insert_legacy_owners(storage: Any, owners: list[str], *, status: str) -> None:
    now = utc_now()
    with storage.transaction() as conn:
        conn.executemany(
            """INSERT INTO users(
                   id,source,external_id,display_name,username,preset_key,status,
                   metadata_json,created_at,updated_at,last_seen_at
               ) VALUES(?,'legacy','','','','user',?,'{}',?,?,?)""",
            [(owner, status, now, now, now) for owner in owners],
        )


def test_worker_state_is_v3_canonical_numeric_and_identity_free() -> None:
    encoded = encode_conversation_passage_worker_state(
        ConversationPassageWorkerState(owner_cursor=17, generation=23)
    )
    decoded, supported = decode_conversation_passage_worker_state(encoded)

    assert supported is True
    assert decoded == ConversationPassageWorkerState(owner_cursor=17, generation=23)
    assert encoded == '{"generation":23,"owner_cursor":17,"version":3}'
    for cursor in (-(2**63), -1, 0, 2**63 - 1):
        boundary = ConversationPassageWorkerState(owner_cursor=cursor, generation=0)
        assert decode_conversation_passage_worker_state(
            encode_conversation_passage_worker_state(boundary)
        ) == (boundary, True)
    for malformed in (
        "",
        "{}",
        '{"generation":0,"owner_cursor":true,"version":3}',
        f'{{"generation":0,"owner_cursor":{-(2**63) - 1},"version":3}}',
        f'{{"generation":0,"owner_cursor":{2**63},"version":3}}',
        '{"generation":0,"owner_cursor":0,"owner_id":"private","version":3}',
        '{"generation":0,"owner_cursor":0,"version":2}',
        ' {"generation":0,"owner_cursor":0,"version":3}',
        '{"generation":0,"generation":1,"owner_cursor":0,"version":3}',
        (f'{{"generation":{CONVERSATION_PASSAGE_MAX_GENERATION + 1},"owner_cursor":0,"version":3}}'),
    ):
        assert decode_conversation_passage_worker_state(malformed)[1] is False


def test_worker_report_cannot_claim_over_budget_bytes_as_budget_reached() -> None:
    report = {
        **_empty_report(),
        "examined": 1,
        "anchors_written": 1,
        "current": 1,
        "message_bytes_examined": CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES + 1,
        "byte_budget_reached": True,
    }

    with pytest.raises(ValueError, match="out of bounds"):
        workers_module._conversation_passage_phase_report(report, limit=64)  # noqa: SLF001


class _OutcomeConversationStorage:
    def __init__(self, outcome: dict[str, object], *, raw_state: str | None = None) -> None:
        self.outcome = outcome
        self.raw_state = raw_state
        self.calls: list[tuple[str | None, int]] = []

    def kv_get(self, key: str) -> str | None:
        assert key == CONVERSATION_PASSAGE_WORKER_STATE_KEY
        return self.raw_state

    def run_conversation_passage_worker_tick(
        self,
        *,
        expected_value: str | None,
        limit: int,
    ) -> dict[str, object]:
        self.calls.append((expected_value, limit))
        return self.outcome


@pytest.mark.asyncio
async def test_worker_service_accepts_an_exact_admitted_report(settings: Any) -> None:
    storage = _OutcomeConversationStorage({"admitted": True, "report": _empty_report(), "phase_error": None})

    await WorkersManager(settings, storage, None, None)._conversation_passage_backfill_one_owner()  # noqa: SLF001

    assert storage.calls == [(None, 64)]


@pytest.mark.asyncio
async def test_worker_service_accepts_an_exact_no_owner_no_report_outcome(settings: Any) -> None:
    storage = _OutcomeConversationStorage({"admitted": True, "report": None, "phase_error": None})

    await WorkersManager(settings, storage, None, None)._conversation_passage_backfill_one_owner()  # noqa: SLF001

    assert storage.calls == [(None, 64)]


@pytest.mark.asyncio
async def test_worker_service_accepts_an_exact_cas_lost_outcome(settings: Any) -> None:
    storage = _OutcomeConversationStorage({"admitted": False, "report": None, "phase_error": None})

    await WorkersManager(settings, storage, None, None)._conversation_passage_backfill_one_owner()  # noqa: SLF001

    assert storage.calls == [(None, 64)]


@pytest.mark.asyncio
async def test_worker_service_surfaces_an_exact_phase_error_outcome(settings: Any) -> None:
    storage = _OutcomeConversationStorage({"admitted": True, "report": None, "phase_error": "LookupError"})

    with pytest.raises(
        workers_module._ConversationPassagePhaseError,  # noqa: SLF001
        match=r"owner phase failed \(LookupError\)",
    ):
        await WorkersManager(settings, storage, None, None)._conversation_passage_backfill_one_owner()  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        {"admitted": 1, "report": None, "phase_error": None},
        {"admitted": False, "report": _empty_report(), "phase_error": None},
        {"admitted": True, "report": _empty_report(), "phase_error": "LookupError"},
        {"admitted": True, "report": None, "phase_error": None, "extra": None},
        {
            "admitted": True,
            "report": {
                **_empty_report(),
                "has_more": True,
                "next_resume_conversation_id": "not-a-canonical-cursor",
            },
            "phase_error": None,
        },
    ],
)
async def test_worker_service_rejects_noncanonical_tick_outcomes(
    settings: Any,
    outcome: dict[str, object],
) -> None:
    storage = _OutcomeConversationStorage(outcome)

    with pytest.raises(RuntimeError, match="result is invalid"):
        await WorkersManager(settings, storage, None, None)._conversation_passage_backfill_one_owner()  # noqa: SLF001


@pytest.mark.asyncio
async def test_existing_worker_slot_rotates_real_person_owners_across_manager_restart(
    settings: Any,
    storage: Any,
) -> None:
    first_owner = "conversation-worker-a"
    second_owner = "conversation-worker-b"
    first = _conversation(storage, first_owner, "first owner")
    second = _conversation(storage, second_owner, "second owner")
    first_rowid = _owner_rowid(storage, first_owner)
    second_rowid = _owner_rowid(storage, second_owner)

    configured = replace(settings, shared_archive=True)
    first_manager = WorkersManager(configured, storage, None, None)
    first_manager.register_all()
    scheduled = [
        task
        for task in first_manager.supervisor._tasks  # noqa: SLF001
        if task.name == "document_catalog_reconcile"
    ]
    assert len(scheduled) == 1
    assert scheduled[0].func == first_manager._retrieval_projection_reconcile_all  # noqa: SLF001

    await scheduled[0].func()
    assert _projection_status(storage, first) == "current"
    assert _projection_status(storage, second) == "incomplete"
    state, supported = decode_conversation_passage_worker_state(
        storage.kv_get(CONVERSATION_PASSAGE_WORKER_STATE_KEY)
    )
    assert supported is True and state == ConversationPassageWorkerState(
        owner_cursor=first_rowid,
        generation=1,
    )

    restarted = WorkersManager(configured, storage, None, None)
    await restarted._conversation_passage_backfill_one_owner()  # noqa: SLF001
    assert _projection_status(storage, second) == "current"
    state, supported = decode_conversation_passage_worker_state(
        storage.kv_get(CONVERSATION_PASSAGE_WORKER_STATE_KEY)
    )
    assert supported is True and state == ConversationPassageWorkerState(
        owner_cursor=second_rowid,
        generation=2,
    )


def test_owner_rotation_wraps_from_the_last_rowid_and_maximum_generation(storage: Any) -> None:
    first_owner = "conversation-worker-wrap-a"
    second_owner = "conversation-worker-wrap-b"
    first_conversation = _conversation(storage, first_owner, "wrap target")
    storage.ensure_user(second_owner)
    first_rowid = _owner_rowid(storage, first_owner)
    second_rowid = _owner_rowid(storage, second_owner)
    initial = encode_conversation_passage_worker_state(
        ConversationPassageWorkerState(
            owner_cursor=second_rowid,
            generation=CONVERSATION_PASSAGE_MAX_GENERATION,
        )
    )
    storage.kv_set(CONVERSATION_PASSAGE_WORKER_STATE_KEY, initial)

    outcome = storage.run_conversation_passage_worker_tick(expected_value=initial, limit=64)

    assert outcome["admitted"] is True
    assert outcome["phase_error"] is None
    assert isinstance(outcome["report"], dict)
    assert _projection_status(storage, first_conversation) == "current"
    state, supported = decode_conversation_passage_worker_state(
        storage.kv_get(CONVERSATION_PASSAGE_WORKER_STATE_KEY)
    )
    assert supported is True and state == ConversationPassageWorkerState(
        owner_cursor=first_rowid,
        generation=0,
    )


def test_owner_tick_filters_only_after_one_fixed_raw_rowid_page(storage: Any) -> None:
    inactive_owners = [
        f"conversation-worker-inactive-{index:02d}"
        for index in range(conversation_storage_module._OWNER_SCAN_PAGE)  # noqa: SLF001
    ]
    for owner in inactive_owners:
        storage.ensure_user(owner)
        storage.update_user(owner, status="disabled")
    active_owner = "conversation-worker-after-raw-page"
    active_conversation = _conversation(storage, active_owner, "bounded raw page")
    last_inactive_rowid = _owner_rowid(storage, inactive_owners[-1])
    active_rowid = _owner_rowid(storage, active_owner)

    first = storage.run_conversation_passage_worker_tick(expected_value=None, limit=64)

    assert first == {"admitted": True, "report": None, "phase_error": None}
    raw_state = storage.kv_get(CONVERSATION_PASSAGE_WORKER_STATE_KEY)
    state, supported = decode_conversation_passage_worker_state(raw_state)
    assert supported is True and state == ConversationPassageWorkerState(
        owner_cursor=last_inactive_rowid,
        generation=1,
    )
    assert _projection_status(storage, active_conversation) == "incomplete"

    second = storage.run_conversation_passage_worker_tick(expected_value=raw_state, limit=64)

    assert second["admitted"] is True and isinstance(second["report"], dict)
    assert second["phase_error"] is None
    state, supported = decode_conversation_passage_worker_state(
        storage.kv_get(CONVERSATION_PASSAGE_WORKER_STATE_KEY)
    )
    assert supported is True and state == ConversationPassageWorkerState(
        owner_cursor=active_rowid,
        generation=2,
    )
    assert _projection_status(storage, active_conversation) == "current"


def test_owner_tick_vm_work_stays_flat_as_unrelated_active_owners_grow(storage: Any) -> None:
    for index in range(conversation_storage_module._OWNER_SCAN_PAGE + 1):  # noqa: SLF001
        storage.ensure_user(f"conversation-worker-vm-base-{index:04d}")

    def measured() -> int:
        with storage.transaction() as conn:
            conn.execute(
                "DELETE FROM runtime_kv WHERE key=?",
                (CONVERSATION_PASSAGE_WORKER_STATE_KEY,),
            )
        instruction_blocks = 0

        def progress() -> int:
            nonlocal instruction_blocks
            instruction_blocks += 1
            return 0

        storage.conn.set_progress_handler(progress, 100)
        try:
            outcome = storage.run_conversation_passage_worker_tick(
                expected_value=None,
                limit=64,
            )
        finally:
            storage.conn.set_progress_handler(None, 0)
        assert outcome["admitted"] is True and outcome["phase_error"] is None
        assert isinstance(outcome["report"], dict)
        return instruction_blocks

    baseline = measured()
    for index in range(1_024):
        storage.ensure_user(f"conversation-worker-vm-growth-{index:04d}")
    grown = measured()

    assert grown <= baseline + max(20, baseline // 4), (baseline, grown)


def test_disabled_invalid_owner_is_filtered_before_id_validation(storage: Any) -> None:
    invalid_owner = "legacy disabled owner with spaces"
    _insert_legacy_owners(storage, [invalid_owner], status="disabled")
    valid_owner = "conversation-worker-after-disabled-invalid"
    valid_conversation = _conversation(storage, valid_owner, "valid after disabled legacy")
    valid_rowid = _owner_rowid(storage, valid_owner)

    outcome = storage.run_conversation_passage_worker_tick(expected_value=None, limit=64)

    assert outcome["admitted"] is True and isinstance(outcome["report"], dict)
    assert outcome["phase_error"] is None
    assert _projection_status(storage, valid_conversation) == "current"
    state, supported = decode_conversation_passage_worker_state(
        storage.kv_get(CONVERSATION_PASSAGE_WORKER_STATE_KEY)
    )
    assert supported is True and state == ConversationPassageWorkerState(
        owner_cursor=valid_rowid,
        generation=1,
    )


def test_active_invalid_page_advances_then_reaches_valid_owner_after_reopen(settings: Any) -> None:
    invalid_owners = [
        f"legacy active owner {index:02d}"
        for index in range(conversation_storage_module._OWNER_SCAN_PAGE)  # noqa: SLF001
    ]
    valid_owner = "conversation-worker-after-active-invalid"
    first = FridayStorage(settings)
    try:
        _insert_legacy_owners(first, invalid_owners, status="active")
        valid_conversation = _conversation(first, valid_owner, "valid after active legacy")
        last_invalid_rowid = _owner_rowid(first, invalid_owners[-1])
        valid_rowid = _owner_rowid(first, valid_owner)

        first_outcome = first.run_conversation_passage_worker_tick(
            expected_value=None,
            limit=64,
        )
        assert first_outcome == {"admitted": True, "report": None, "phase_error": None}
        expected_value = first.kv_get(CONVERSATION_PASSAGE_WORKER_STATE_KEY)
        state, supported = decode_conversation_passage_worker_state(expected_value)
        assert supported is True and state == ConversationPassageWorkerState(
            owner_cursor=last_invalid_rowid,
            generation=1,
        )
        assert _projection_status(first, valid_conversation) == "incomplete"
    finally:
        first.close()

    reopened = FridayStorage(replace(settings, database_must_exist=True))
    try:
        second_outcome = reopened.run_conversation_passage_worker_tick(
            expected_value=expected_value,
            limit=64,
        )
        assert second_outcome["admitted"] is True
        assert isinstance(second_outcome["report"], dict)
        assert second_outcome["phase_error"] is None
        state, supported = decode_conversation_passage_worker_state(
            reopened.kv_get(CONVERSATION_PASSAGE_WORKER_STATE_KEY)
        )
        assert supported is True and state == ConversationPassageWorkerState(
            owner_cursor=valid_rowid,
            generation=2,
        )
        assert _projection_status(reopened, valid_conversation) == "current"
    finally:
        reopened.close()


def test_rotation_skips_deactivated_and_deleted_rows_after_database_reopen(settings: Any) -> None:
    first_owner = "conversation-worker-reopen-a"
    deactivated_owner = "conversation-worker-reopen-b"
    deleted_owner = "conversation-worker-reopen-c"
    surviving_owner = "conversation-worker-reopen-d"
    first = FridayStorage(settings)
    try:
        for owner in (first_owner, deactivated_owner, deleted_owner, surviving_owner):
            first.ensure_user(owner)
        first_rowid = _owner_rowid(first, first_owner)
        surviving_rowid = _owner_rowid(first, surviving_owner)
        admitted = first.run_conversation_passage_worker_tick(expected_value=None, limit=64)
        assert admitted["admitted"] is True and isinstance(admitted["report"], dict)
        state, supported = decode_conversation_passage_worker_state(
            first.kv_get(CONVERSATION_PASSAGE_WORKER_STATE_KEY)
        )
        assert supported is True and state.owner_cursor == first_rowid
        first.update_user(deactivated_owner, status="disabled")
        with first.transaction() as conn:
            conn.execute("DELETE FROM users WHERE id=?", (deleted_owner,))
        expected_value = first.kv_get(CONVERSATION_PASSAGE_WORKER_STATE_KEY)
    finally:
        first.close()

    reopened = FridayStorage(replace(settings, database_must_exist=True))
    try:
        outcome = reopened.run_conversation_passage_worker_tick(
            expected_value=expected_value,
            limit=64,
        )
        assert outcome["admitted"] is True and isinstance(outcome["report"], dict)
        assert outcome["phase_error"] is None
        state, supported = decode_conversation_passage_worker_state(
            reopened.kv_get(CONVERSATION_PASSAGE_WORKER_STATE_KEY)
        )
        assert supported is True and state == ConversationPassageWorkerState(
            owner_cursor=surviving_rowid,
            generation=2,
        )
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_composite_worker_runs_sibling_phase_after_document_failure(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = WorkersManager(settings, object(), None, None)
    calls: list[str] = []

    async def broken_document() -> None:
        calls.append("document")
        raise RuntimeError("private body must not reach worker health")

    async def healthy_conversation() -> None:
        calls.append("conversation")

    monkeypatch.setattr(manager, "_document_catalog_reconcile_all", broken_document)
    monkeypatch.setattr(manager, "_conversation_passage_backfill_one_owner", healthy_conversation)

    with pytest.raises(WorkerBatchError, match="1 retrieval projection phase"):
        await manager._retrieval_projection_reconcile_all()  # noqa: SLF001
    assert sorted(calls) == ["conversation", "document"]


@pytest.mark.asyncio
async def test_composite_launches_conversation_phase_before_a_document_phase_can_strand(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = WorkersManager(settings, object(), None, None)
    document_started = asyncio.Event()
    conversation_finished = asyncio.Event()
    never_release = asyncio.Event()

    async def stranded_document() -> None:
        document_started.set()
        await never_release.wait()

    async def healthy_conversation() -> None:
        conversation_finished.set()

    monkeypatch.setattr(manager, "_document_catalog_reconcile_all", stranded_document)
    monkeypatch.setattr(manager, "_conversation_passage_backfill_one_owner", healthy_conversation)
    task = asyncio.create_task(manager._retrieval_projection_reconcile_all())  # noqa: SLF001
    await document_started.wait()
    await conversation_finished.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_two_managers_admit_only_one_physical_owner_tick(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = "conversation-worker-concurrent"
    conversation = _conversation(storage, owner, "single admission")
    owner_rowid = _owner_rowid(storage, owner)
    barrier = threading.Barrier(2)
    real_get = storage.kv_get

    def synchronized_get(key: str) -> str | None:
        value = real_get(key)
        barrier.wait(timeout=5)
        return value

    monkeypatch.setattr(storage, "kv_get", synchronized_get)
    first = WorkersManager(settings, storage, None, None)
    second = WorkersManager(settings, storage, None, None)

    await asyncio.gather(
        first._conversation_passage_backfill_one_owner(),  # noqa: SLF001
        second._conversation_passage_backfill_one_owner(),  # noqa: SLF001
    )

    state, supported = decode_conversation_passage_worker_state(
        real_get(CONVERSATION_PASSAGE_WORKER_STATE_KEY)
    )
    assert supported is True and state == ConversationPassageWorkerState(
        owner_cursor=owner_rowid,
        generation=1,
    )
    assert _projection_status(storage, conversation) == "current"
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM conversation_passages WHERE conversation_id=?",
            (conversation,),
        ).fetchone()[0]
        == 1
    )


class _BlockingConversationStorage:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.tick_calls = 0

    def kv_get(self, key: str) -> None:
        assert key == CONVERSATION_PASSAGE_WORKER_STATE_KEY
        return None

    def run_conversation_passage_worker_tick(
        self,
        *,
        expected_value: str | None,
        limit: int,
    ) -> dict[str, object]:
        assert expected_value is None and limit == 64
        self.tick_calls += 1
        self.started.set()
        self.release.wait(timeout=5)
        self.finished.set()
        return {"admitted": True, "phase_error": None, "report": _empty_report()}


@pytest.mark.asyncio
async def test_cancellation_never_retries_a_sqlite_tick_that_can_still_commit(
    settings: Any,
) -> None:
    storage = _BlockingConversationStorage()
    manager = WorkersManager(settings, storage, None, None)
    task = asyncio.create_task(manager._conversation_passage_backfill_one_owner())  # noqa: SLF001
    assert await asyncio.to_thread(storage.started.wait, 2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    storage.release.set()
    assert await asyncio.to_thread(storage.finished.wait, 2)
    await asyncio.sleep(0)
    assert storage.tick_calls == 1
