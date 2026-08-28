from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, TypedDict

import pytest

from friday.interaction_control_plane.engineer_work_item import (
    EngineerWorkItemChannel,
    EngineerWorkItemConflictError,
    EngineerWorkItemState,
    EngineerWorkItemStepState,
    get_current_engineer_work_item_in_transaction,
    get_engineer_work_item_in_transaction,
)
from friday.orchestration.engineer_work_item_coordinator import (
    EngineerCommandLedgerDisposition,
    EngineerCommandReservation,
    EngineerCommandSourceSlot,
    EngineerWorkItemCoordinatorError,
    EngineerWorkItemRuntimeCoordinator,
)
from friday.organs.engineer.command.contracts import CommandStatus
from friday.organs.engineer.command.store import CommandJobStore

OWNER = "engineer-coordinator-owner"
TENANT = "engineer-coordinator-tenant"
DELIVERY_CHAT_ID = "424242"
NOW = "2026-08-27T12:00:00+00:00"
LATER = "2026-08-27T12:00:01+00:00"
EXPIRY = "2026-08-27T23:59:59+00:00"
TERMINAL = "e" * 64


class _Scope(TypedDict):
    owner_id: str
    tenant_id: str
    conversation_id: str
    channel: EngineerWorkItemChannel


class _FakeCommandLedger:
    def __init__(self) -> None:
        self.bindings: dict[tuple[str, str], dict[str, object]] = {}
        self.jobs: dict[str, dict[str, object]] = {}
        self.fences_by_key: dict[tuple[str, str], dict[str, object]] = {}
        self.fences_by_source: dict[tuple[str, str], dict[str, object]] = {}
        self.source_slots_by_binding: dict[tuple[str, str], dict[str, object]] = {}
        self.source_slots_by_key: dict[tuple[str, str], dict[str, object]] = {}
        self.fail: set[str] = set()
        self.fail_after_fence_once = False
        self.last_fence_legacy_source_binding: str | None = None

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise OSError(f"synthetic {name} failure")

    def assert_lifecycle_ready(self) -> None:
        self._maybe_fail("assert_lifecycle_ready")

    def admit(
        self,
        reservation: EngineerCommandReservation,
        *,
        job_id: str,
        status: CommandStatus,
    ) -> None:
        source = reservation.source
        binding: dict[str, object] = {
            "job_id": job_id,
            "actor_id": source.owner_id,
            "tenant_id": source.tenant_id,
            "conversation_id": source.conversation_id,
            "channel": source.channel.value,
            "source_row_id": source.source_row_id,
            "source_step_id": source.source_step_id,
            "source_hash": source.source_hash,
            "telegram_update_id": source.telegram_update_id,
            "idempotency_key": reservation.idempotency_key,
            "command_digest": reservation.command_digest,
            "delivery_chat_id": source.delivery_chat_id,
        }
        self.bindings[(source.owner_id, reservation.idempotency_key)] = binding
        self.jobs[job_id] = {
            **{key: value for key, value in binding.items() if key != "delivery_chat_id"},
            "status": status.value,
            # The coordinator must never project these external private fields.
            "argv_json": '["private-command"]',
            "stdout": b"PRIVATE_STDOUT",
            "stderr": b"PRIVATE_STDERR",
        }
        slot: dict[str, object] = {
            "actor_id": source.owner_id,
            "source_binding_sha256": source.binding_sha256(),
            # New v2 rows use v1 only as a conservative lookup alias. They do
            # not persist it, otherwise two distinct call slots would collide.
            "legacy_source_binding_sha256": None,
            "idempotency_key": reservation.idempotency_key,
            "command_digest": reservation.command_digest,
            "target_kind": "job",
            "job_id": job_id,
            "fence_actor_id": None,
            "fence_idempotency_key": None,
            "work_item_id": None,
            "expected_revision": None,
            "step_ordinal": None,
        }
        self.source_slots_by_binding[(source.owner_id, source.binding_sha256())] = slot
        self.source_slots_by_key[(source.owner_id, reservation.idempotency_key)] = slot

    def set_status(self, job_id: str, status: CommandStatus) -> None:
        self.jobs[job_id]["status"] = status.value

    def seed_legacy_source_collision(self, source: EngineerCommandSourceSlot) -> None:
        legacy = source.legacy_binding_sha256()
        legacy_key = "ecmd-" + "f" * 64
        slot: dict[str, object] = {
            "actor_id": source.owner_id,
            "source_binding_sha256": legacy,
            "legacy_source_binding_sha256": legacy,
            "idempotency_key": legacy_key,
            "command_digest": "a" * 64,
            "target_kind": "job",
            "job_id": "f" * 32,
            "fence_actor_id": None,
            "fence_idempotency_key": None,
            "work_item_id": None,
            "expected_revision": None,
            "step_ordinal": None,
        }
        self.source_slots_by_binding[(source.owner_id, legacy)] = slot
        self.source_slots_by_key[(source.owner_id, legacy_key)] = slot

    def lookup_idempotency_binding(
        self,
        actor_id: str,
        key: str,
    ) -> dict[str, object] | None:
        self._maybe_fail("lookup_idempotency_binding")
        value = self.bindings.get((actor_id, key))
        return None if value is None else dict(value)

    def lookup_engineer_command_source_slot(
        self,
        actor_id: str,
        source_binding_sha256: str,
        *,
        legacy_source_binding_sha256: str | None = None,
    ) -> dict[str, object] | None:
        self._maybe_fail("lookup_engineer_command_source_slot")
        value = self.source_slots_by_binding.get((actor_id, source_binding_sha256))
        if value is None and legacy_source_binding_sha256 is not None:
            value = next(
                (
                    slot
                    for (slot_actor, _binding), slot in self.source_slots_by_binding.items()
                    if slot_actor == actor_id
                    and slot.get("legacy_source_binding_sha256") == legacy_source_binding_sha256
                ),
                None,
            )
        return None if value is None else dict(value)

    def lookup_engineer_command_source_slot_by_key(
        self,
        actor_id: str,
        idempotency_key: str,
    ) -> dict[str, object] | None:
        self._maybe_fail("lookup_engineer_command_source_slot_by_key")
        value = self.source_slots_by_key.get((actor_id, idempotency_key))
        return None if value is None else dict(value)

    def lookup_engineer_work_item_fence(
        self,
        actor_id: str,
        idempotency_key: str,
    ) -> dict[str, object] | None:
        self._maybe_fail("lookup_engineer_work_item_fence")
        value = self.fences_by_key.get((actor_id, idempotency_key))
        return None if value is None else dict(value)

    def lookup_engineer_work_item_fence_by_source(
        self,
        actor_id: str,
        source_binding_sha256: str,
    ) -> dict[str, object] | None:
        self._maybe_fail("lookup_engineer_work_item_fence_by_source")
        value = self.fences_by_source.get((actor_id, source_binding_sha256))
        return None if value is None else dict(value)

    def create_engineer_work_item_fence(
        self,
        *,
        actor_id: str,
        idempotency_key: str,
        work_item_id: str,
        expected_revision: int,
        step_ordinal: int,
        source_binding_sha256: str,
        legacy_source_binding_sha256: str | None = None,
        command_digest: str,
        created_at: float | None = None,
    ) -> dict[str, object]:
        del created_at
        self._maybe_fail("create_engineer_work_item_fence")
        self.last_fence_legacy_source_binding = legacy_source_binding_sha256
        if (actor_id, idempotency_key) in self.bindings:
            raise RuntimeError("idempotency_conflict")
        source_collision = self.lookup_engineer_command_source_slot(
            actor_id,
            source_binding_sha256,
            legacy_source_binding_sha256=legacy_source_binding_sha256,
        )
        key_collision = self.lookup_engineer_command_source_slot_by_key(
            actor_id,
            idempotency_key,
        )
        if source_collision is not None or key_collision is not None:
            raise RuntimeError("engineer_command_source_slot_conflict")
        fence: dict[str, object] = {
            "actor_id": actor_id,
            "work_item_id": work_item_id,
            "expected_revision": expected_revision,
            "step_ordinal": step_ordinal,
            "source_binding_sha256": source_binding_sha256,
            "idempotency_key": idempotency_key,
            "command_digest": command_digest,
        }
        self.fences_by_key[(actor_id, idempotency_key)] = fence
        self.fences_by_source[(actor_id, source_binding_sha256)] = fence
        slot: dict[str, object] = {
            "actor_id": actor_id,
            "source_binding_sha256": source_binding_sha256,
            "legacy_source_binding_sha256": None,
            "idempotency_key": idempotency_key,
            "command_digest": command_digest,
            "target_kind": "engineer_work_item_fence",
            "job_id": None,
            "fence_actor_id": actor_id,
            "fence_idempotency_key": idempotency_key,
            "work_item_id": work_item_id,
            "expected_revision": expected_revision,
            "step_ordinal": step_ordinal,
        }
        self.source_slots_by_binding[(actor_id, source_binding_sha256)] = slot
        self.source_slots_by_key[(actor_id, idempotency_key)] = slot
        if self.fail_after_fence_once:
            self.fail_after_fence_once = False
            raise OSError("synthetic lost fence acknowledgement")
        return dict(fence)

    def read_job(self, job_id: str) -> dict[str, object]:
        self._maybe_fail("read_job")
        try:
            return dict(self.jobs[job_id])
        except KeyError as exc:
            raise RuntimeError("job_not_found") from exc


def _scope(storage: Any) -> tuple[str, _Scope]:
    storage.ensure_user(OWNER)
    storage.ensure_user(TENANT)
    conversation = storage.create_conversation(OWNER, title="coordinator")
    conversation_id = str(conversation["id"])
    return conversation_id, {
        "owner_id": OWNER,
        "tenant_id": TENANT,
        "conversation_id": conversation_id,
        "channel": EngineerWorkItemChannel.TELEGRAM,
    }


def _reservation(
    conversation_id: str,
    *,
    ordinal: int = 1,
    command_digit: str = "c",
) -> EngineerCommandReservation:
    source = EngineerCommandSourceSlot(
        owner_id=OWNER,
        tenant_id=TENANT,
        conversation_id=conversation_id,
        channel=EngineerWorkItemChannel.TELEGRAM,
        source_row_id=f"msg_{ordinal:016x}",
        source_step_id=f"ecstep-{ordinal:032x}",
        source_hash=f"{ordinal:064x}",
        telegram_update_id=str(10_000 + ordinal),
        delivery_chat_id=DELIVERY_CHAT_ID,
    )
    return EngineerCommandReservation(
        source=source,
        idempotency_key="ecmd-" + f"{ordinal:064x}",
        command_digest=command_digit * 64,
    )


def _item(storage: Any, scope: _Scope, work_item_id: str) -> Any:
    return get_engineer_work_item_in_transaction(
        storage.conn,
        **scope,
        work_item_id=work_item_id,
    )


def test_initial_reservation_reconciles_exact_external_admission(storage: Any) -> None:
    conversation_id, scope = _scope(storage)
    ledger = _FakeCommandLedger()
    coordinator = EngineerWorkItemRuntimeCoordinator(ledger)
    reservation = _reservation(conversation_id)

    with storage.transaction() as conn:
        reserved = coordinator.reserve_initial_in_transaction(
            conn,
            reservation=reservation,
            now=NOW,
            expires_at=EXPIRY,
        )
    assert reserved.can_submit
    assert reserved.disposition is EngineerCommandLedgerDisposition.ABSENT
    assert reserved.continuation is not None
    assert reserved.continuation.state is EngineerWorkItemState.ACTIVE
    work_item_id = reserved.continuation.work_item_id

    ledger.admit(
        reservation,
        job_id="1" * 32,
        status=CommandStatus.RUNNING,
    )
    with storage.transaction() as conn:
        admitted = coordinator.reconcile_admission_in_transaction(
            conn,
            **scope,
            work_item_id=work_item_id,
            expected_revision=reserved.continuation.revision,
            source=reservation.source,
            now=LATER,
        )
    assert not admitted.can_submit
    assert admitted.disposition is EngineerCommandLedgerDisposition.EXACT
    assert admitted.continuation is not None
    assert admitted.continuation.state is EngineerWorkItemState.WAITING_FOR_CAPABILITY
    assert admitted.continuation.command_job_id == "1" * 32
    assert admitted.continuation.command_status is CommandStatus.RUNNING
    assert admitted.continuation.job_receipt_sha256


def test_real_command_store_projection_reconciles_the_admission(
    storage: Any,
    tmp_path: Path,
) -> None:
    conversation_id, _scope_value = _scope(storage)
    reservation = _reservation(conversation_id)
    ledger = CommandJobStore.provision(tmp_path / "command-ledger")
    try:
        ledger.insert_job(
            {
                "job_id": "7" * 32,
                "actor_id": reservation.source.owner_id,
                "tenant_id": reservation.source.tenant_id,
                "conversation_id": reservation.source.conversation_id,
                "channel": reservation.source.channel.value,
                "source_row_id": reservation.source.source_row_id,
                "source_step_id": reservation.source.source_step_id,
                "source_hash": reservation.source.source_hash,
                "telegram_update_id": reservation.source.telegram_update_id,
                "isolation_profile": "host_user",
                "host_user_authorized": True,
                "idempotency_key": reservation.idempotency_key,
                "command_digest": reservation.command_digest,
                "input_manifest_sha256": "",
                "argv_sha256": "a" * 64,
                "lane": "shell",
                "origin": "model",
                "status": CommandStatus.ADMITTED.value,
                "error_code": "",
                "grant_nonce": "coordinator-real-store-test",
                "timeout_sec": 300,
                "max_stdout_bytes": 1024,
                "max_stderr_bytes": 1024,
                "created_at": 1_777_000_000.0,
                "executable_json": None,
                "delivery_chat_id": reservation.source.delivery_chat_id,
            }
        )
        coordinator = EngineerWorkItemRuntimeCoordinator(ledger)
        with storage.transaction() as conn:
            outcome = coordinator.reserve_initial_in_transaction(
                conn,
                reservation=reservation,
                now=NOW,
                expires_at=EXPIRY,
            )
        assert outcome.disposition is EngineerCommandLedgerDisposition.EXACT
        assert outcome.continuation is not None
        assert outcome.continuation.state is EngineerWorkItemState.WAITING_FOR_CAPABILITY
        assert outcome.continuation.command_job_id == "7" * 32
        assert outcome.continuation.command_status is CommandStatus.ADMITTED
    finally:
        ledger.close()


def test_strict_runtime_fence_receives_conservative_legacy_lookup_digest(
    storage: Any,
    tmp_path: Path,
) -> None:
    conversation_id, scope = _scope(storage)
    reservation = _reservation(conversation_id)
    root = tmp_path / "strict-command-ledger"
    anchor = tmp_path / "strict-command-anchor"
    lifecycle_key = b"C" * 32
    provisioned = CommandJobStore.provision(
        root,
        lifecycle_key=lifecycle_key,
        lifecycle_state_dir=anchor,
    )
    provisioned.close()
    ledger = CommandJobStore.open_runtime(
        root,
        lifecycle_key=lifecycle_key,
        lifecycle_state_dir=anchor,
    )
    try:
        coordinator = EngineerWorkItemRuntimeCoordinator(ledger)
        with storage.transaction() as conn:
            reserved = coordinator.reserve_initial_in_transaction(
                conn,
                reservation=reservation,
                now=NOW,
                expires_at=EXPIRY,
            )
        assert reserved.continuation is not None
        with storage.transaction() as conn:
            retired = coordinator.retire_proven_unsubmitted_in_transaction(
                conn,
                **scope,
                work_item_id=reserved.continuation.work_item_id,
                expected_revision=reserved.continuation.revision,
                source=reservation.source,
                now=LATER,
            )
        assert retired.disposition is EngineerCommandLedgerDisposition.FENCED
        slot = ledger.lookup_engineer_command_source_slot(
            OWNER,
            reservation.source.binding_sha256(),
            legacy_source_binding_sha256=reservation.source.legacy_binding_sha256(),
        )
        assert slot is not None
        assert slot["target_kind"] == "engineer_work_item_fence"
        assert slot["legacy_source_binding_sha256"] is None
    finally:
        ledger.close()


def test_unknown_requires_proven_admission_and_never_infers_it_from_failure(storage: Any) -> None:
    conversation_id, scope = _scope(storage)
    ledger = _FakeCommandLedger()
    coordinator = EngineerWorkItemRuntimeCoordinator(ledger)
    reservation = _reservation(conversation_id)
    with storage.transaction() as conn:
        reserved = coordinator.reserve_initial_in_transaction(
            conn,
            reservation=reservation,
            now=NOW,
            expires_at=EXPIRY,
        )
    assert reserved.continuation is not None

    with (
        pytest.raises(
            EngineerWorkItemCoordinatorError,
            match="command_admission_unproven",
        ),
        storage.transaction() as conn,
    ):
        coordinator.mark_unknown_in_transaction(
            conn,
            **scope,
            work_item_id=reserved.continuation.work_item_id,
            expected_revision=reserved.continuation.revision,
            source=reservation.source,
            now=LATER,
        )
    unchanged = _item(storage, scope, reserved.continuation.work_item_id)
    assert unchanged.state is EngineerWorkItemState.ACTIVE
    assert unchanged.current_step.job_receipt_sha256 == ""

    ledger.admit(
        reservation,
        job_id="2" * 32,
        status=CommandStatus.UNKNOWN,
    )
    with storage.transaction() as conn:
        unknown = coordinator.mark_unknown_in_transaction(
            conn,
            **scope,
            work_item_id=unchanged.id,
            expected_revision=unchanged.revision,
            source=reservation.source,
            now=LATER,
        )
    assert unknown.state is EngineerWorkItemState.UNCERTAIN
    assert unknown.step_state is EngineerWorkItemStepState.UNKNOWN
    assert unknown.command_status is CommandStatus.UNKNOWN

    with storage.transaction() as conn:
        replay = coordinator.mark_unknown_in_transaction(
            conn,
            **scope,
            work_item_id=unknown.work_item_id,
            expected_revision=unknown.revision,
            now=LATER,
        )
    assert replay == unknown


def test_cross_database_read_failure_rolls_back_new_main_reservation(storage: Any) -> None:
    conversation_id, scope = _scope(storage)
    ledger = _FakeCommandLedger()
    ledger.fail.add("lookup_idempotency_binding")
    coordinator = EngineerWorkItemRuntimeCoordinator(ledger)

    # The coordinator's own savepoint must undo the new item even when a
    # higher-level caller catches the error and commits unrelated main work.
    with storage.transaction() as conn:
        with pytest.raises(
            EngineerWorkItemCoordinatorError,
            match="command_ledger_unavailable",
        ):
            coordinator.reserve_initial_in_transaction(
                conn,
                reservation=_reservation(conversation_id),
                now=NOW,
                expires_at=EXPIRY,
            )
        conn.execute("CREATE TABLE coordinator_unrelated_commit(value TEXT)")
    assert get_current_engineer_work_item_in_transaction(storage.conn, **scope) is None
    assert storage.execute("SELECT COUNT(*) FROM coordinator_unrelated_commit").fetchone()[0] == 0


def test_prepared_reconciliation_requires_the_exact_source_for_legacy_lookup(storage: Any) -> None:
    conversation_id, scope = _scope(storage)
    coordinator = EngineerWorkItemRuntimeCoordinator(_FakeCommandLedger())
    reservation = _reservation(conversation_id)
    with storage.transaction() as conn:
        reserved = coordinator.reserve_initial_in_transaction(
            conn,
            reservation=reservation,
            now=NOW,
            expires_at=EXPIRY,
        )
    assert reserved.continuation is not None

    with (
        pytest.raises(
            EngineerWorkItemCoordinatorError,
            match="command_source_required",
        ),
        storage.transaction() as conn,
    ):
        coordinator.reconcile_admission_in_transaction(
            conn,
            **scope,
            work_item_id=reserved.continuation.work_item_id,
            expected_revision=reserved.continuation.revision,
            now=LATER,
        )


def test_true_legacy_v1_source_collision_is_never_treated_as_absence(storage: Any) -> None:
    conversation_id, scope = _scope(storage)
    ledger = _FakeCommandLedger()
    reservation = _reservation(conversation_id)
    ledger.seed_legacy_source_collision(reservation.source)
    coordinator = EngineerWorkItemRuntimeCoordinator(ledger)

    with (
        pytest.raises(
            EngineerWorkItemCoordinatorError,
            match="legacy_command_source_collision",
        ),
        storage.transaction() as conn,
    ):
        coordinator.reserve_initial_in_transaction(
            conn,
            reservation=reservation,
            now=NOW,
            expires_at=EXPIRY,
        )
    assert get_current_engineer_work_item_in_transaction(storage.conn, **scope) is None


def test_distinct_v2_steps_do_not_persist_or_match_a_false_v1_alias(storage: Any) -> None:
    conversation_id, scope = _scope(storage)
    ledger = _FakeCommandLedger()
    coordinator = EngineerWorkItemRuntimeCoordinator(ledger)
    first = _reservation(conversation_id)
    with storage.transaction() as conn:
        reserved = coordinator.reserve_initial_in_transaction(
            conn,
            reservation=first,
            now=NOW,
            expires_at=EXPIRY,
        )
    assert reserved.continuation is not None
    with storage.transaction() as conn:
        retired = coordinator.retire_proven_unsubmitted_in_transaction(
            conn,
            **scope,
            work_item_id=reserved.continuation.work_item_id,
            expected_revision=reserved.continuation.revision,
            source=first.source,
            now=LATER,
        )
    assert retired.continuation is None
    assert ledger.last_fence_legacy_source_binding == first.source.legacy_binding_sha256()
    first_slot = ledger.source_slots_by_key[(OWNER, first.idempotency_key)]
    assert first_slot["legacy_source_binding_sha256"] is None

    second_source = replace(first.source, source_step_id="ecstep-" + "2" * 32)
    second = EngineerCommandReservation(
        source=second_source,
        idempotency_key="ecmd-" + "2" * 64,
        command_digest="d" * 64,
    )
    assert second.source.legacy_binding_sha256() == first.source.legacy_binding_sha256()
    assert second.source.binding_sha256() != first.source.binding_sha256()
    with storage.transaction() as conn:
        second_reserved = coordinator.reserve_initial_in_transaction(
            conn,
            reservation=second,
            now="2026-08-27T12:00:02+00:00",
            expires_at=EXPIRY,
        )
    assert second_reserved.can_submit


def test_mismatched_external_scope_fails_closed_without_binding(storage: Any) -> None:
    conversation_id, scope = _scope(storage)
    ledger = _FakeCommandLedger()
    coordinator = EngineerWorkItemRuntimeCoordinator(ledger)
    reservation = _reservation(conversation_id)
    with storage.transaction() as conn:
        reserved = coordinator.reserve_initial_in_transaction(
            conn,
            reservation=reservation,
            now=NOW,
            expires_at=EXPIRY,
        )
    assert reserved.continuation is not None
    ledger.admit(reservation, job_id="3" * 32, status=CommandStatus.RUNNING)
    binding = ledger.bindings[(OWNER, reservation.idempotency_key)]
    binding["conversation_id"] = "conv_ffffffffffffffff"

    with (
        pytest.raises(
            EngineerWorkItemCoordinatorError,
            match="command_ledger_inconsistent",
        ),
        storage.transaction() as conn,
    ):
        coordinator.reconcile_admission_in_transaction(
            conn,
            **scope,
            work_item_id=reserved.continuation.work_item_id,
            expected_revision=reserved.continuation.revision,
            source=reservation.source,
            now=LATER,
        )
    unchanged = _item(storage, scope, reserved.continuation.work_item_id)
    assert unchanged.state is EngineerWorkItemState.ACTIVE
    assert unchanged.current_step.job_receipt_sha256 == ""


def test_admitted_main_state_fails_closed_if_external_truth_disappears(storage: Any) -> None:
    conversation_id, scope = _scope(storage)
    ledger = _FakeCommandLedger()
    coordinator = EngineerWorkItemRuntimeCoordinator(ledger)
    reservation = _reservation(conversation_id)
    with storage.transaction() as conn:
        reserved = coordinator.reserve_initial_in_transaction(
            conn,
            reservation=reservation,
            now=NOW,
            expires_at=EXPIRY,
        )
    assert reserved.continuation is not None
    ledger.admit(reservation, job_id="8" * 32, status=CommandStatus.RUNNING)
    with storage.transaction() as conn:
        admitted = coordinator.reconcile_admission_in_transaction(
            conn,
            **scope,
            work_item_id=reserved.continuation.work_item_id,
            expected_revision=reserved.continuation.revision,
            source=reservation.source,
            now=LATER,
        )
    assert admitted.continuation is not None
    ledger.bindings.clear()
    ledger.jobs.clear()

    with (
        pytest.raises(
            EngineerWorkItemCoordinatorError,
            match="command_ledger_lost",
        ),
        storage.transaction() as conn,
    ):
        coordinator.current_structural_state_in_transaction(conn, **scope)
    durable = _item(storage, scope, admitted.continuation.work_item_id)
    assert durable.state is EngineerWorkItemState.WAITING_FOR_CAPABILITY
    assert durable.current_step.job_receipt_sha256


def test_verified_terminal_next_step_fence_and_atomic_completion(storage: Any) -> None:
    conversation_id, scope = _scope(storage)
    ledger = _FakeCommandLedger()
    coordinator = EngineerWorkItemRuntimeCoordinator(ledger)
    first = _reservation(conversation_id)
    with storage.transaction() as conn:
        reserved = coordinator.reserve_initial_in_transaction(
            conn,
            reservation=first,
            now=NOW,
            expires_at=EXPIRY,
        )
    assert reserved.continuation is not None
    ledger.admit(first, job_id="4" * 32, status=CommandStatus.COMPLETED)

    with storage.transaction() as conn:
        settled = coordinator.settle_verified_terminal_in_transaction(
            conn,
            **scope,
            work_item_id=reserved.continuation.work_item_id,
            expected_revision=reserved.continuation.revision,
            verified_job_id="4" * 32,
            verified_terminal_receipt_sha256=TERMINAL,
            source=first.source,
            now=LATER,
        )
    assert settled.state is EngineerWorkItemState.WAITING_FOR_INPUT
    assert settled.terminal_receipt_sha256 == TERMINAL

    second = _reservation(conversation_id, ordinal=2, command_digit="d")
    with storage.transaction() as conn:
        next_step = coordinator.reserve_next_in_transaction(
            conn,
            work_item_id=settled.work_item_id,
            expected_revision=settled.revision,
            reservation=second,
            now="2026-08-27T12:00:02+00:00",
        )
    assert next_step.can_submit
    assert next_step.continuation is not None
    assert next_step.continuation.step_ordinal == 2

    with storage.transaction() as conn:
        retired = coordinator.retire_proven_unsubmitted_in_transaction(
            conn,
            **scope,
            work_item_id=next_step.continuation.work_item_id,
            expected_revision=next_step.continuation.revision,
            source=second.source,
            now="2026-08-27T12:00:03+00:00",
        )
    assert retired.disposition is EngineerCommandLedgerDisposition.FENCED
    assert retired.continuation is not None
    assert retired.continuation.state is EngineerWorkItemState.WAITING_FOR_INPUT
    assert retired.continuation.step_ordinal == 1

    with storage.transaction() as conn:
        ready = coordinator.prepare_completion_in_transaction(
            conn,
            **scope,
            work_item_id=retired.continuation.work_item_id,
            expected_revision=retired.continuation.revision,
            now="2026-08-27T12:00:04+00:00",
        )
    assert ready.state is EngineerWorkItemState.READY_TO_ANSWER
    storage.execute("CREATE TABLE coordinator_publication_marker(value TEXT NOT NULL)")
    storage.conn.commit()

    with pytest.raises(RuntimeError, match="synthetic publication rollback"), storage.transaction() as conn:
        conn.execute("INSERT INTO coordinator_publication_marker(value) VALUES('rolled-back')")
        coordinator.close_completion_in_transaction(
            conn,
            **scope,
            work_item_id=ready.work_item_id,
            expected_revision=ready.revision,
            now="2026-08-27T12:00:05+00:00",
        )
        raise RuntimeError("synthetic publication rollback")
    assert storage.execute("SELECT COUNT(*) FROM coordinator_publication_marker").fetchone()[0] == 0
    durable_ready = _item(storage, scope, ready.work_item_id)
    assert durable_ready.state is EngineerWorkItemState.READY_TO_ANSWER

    with storage.transaction() as conn:
        conn.execute("INSERT INTO coordinator_publication_marker(value) VALUES('published')")
        completed = coordinator.close_completion_in_transaction(
            conn,
            **scope,
            work_item_id=ready.work_item_id,
            expected_revision=ready.revision,
            now="2026-08-27T12:00:05+00:00",
        )
    assert completed.state is EngineerWorkItemState.COMPLETED
    assert storage.execute("SELECT value FROM coordinator_publication_marker").fetchone()[0] == "published"


def test_fence_acknowledgement_loss_never_reopens_submission(storage: Any) -> None:
    conversation_id, scope = _scope(storage)
    ledger = _FakeCommandLedger()
    coordinator = EngineerWorkItemRuntimeCoordinator(ledger)
    reservation = _reservation(conversation_id)
    with storage.transaction() as conn:
        reserved = coordinator.reserve_initial_in_transaction(
            conn,
            reservation=reservation,
            now=NOW,
            expires_at=EXPIRY,
        )
    assert reserved.continuation is not None
    ledger.fail_after_fence_once = True

    with (
        pytest.raises(
            EngineerWorkItemCoordinatorError,
            match="command_ledger_unavailable",
        ),
        storage.transaction() as conn,
    ):
        coordinator.retire_proven_unsubmitted_in_transaction(
            conn,
            **scope,
            work_item_id=reserved.continuation.work_item_id,
            expected_revision=reserved.continuation.revision,
            source=reservation.source,
            now=LATER,
        )
    still_prepared = _item(storage, scope, reserved.continuation.work_item_id)
    assert still_prepared.state is EngineerWorkItemState.ACTIVE

    with storage.transaction() as conn:
        recovered = coordinator.reconcile_admission_in_transaction(
            conn,
            **scope,
            work_item_id=still_prepared.id,
            expected_revision=still_prepared.revision,
            source=reservation.source,
            now=LATER,
        )
    assert recovered.disposition is EngineerCommandLedgerDisposition.FENCED
    assert recovered.continuation is None
    assert get_current_engineer_work_item_in_transaction(storage.conn, **scope) is None


def test_unverified_unknown_is_not_a_settleable_terminal(storage: Any) -> None:
    conversation_id, scope = _scope(storage)
    ledger = _FakeCommandLedger()
    coordinator = EngineerWorkItemRuntimeCoordinator(ledger)
    reservation = _reservation(conversation_id)
    with storage.transaction() as conn:
        reserved = coordinator.reserve_initial_in_transaction(
            conn,
            reservation=reservation,
            now=NOW,
            expires_at=EXPIRY,
        )
    assert reserved.continuation is not None
    ledger.admit(reservation, job_id="5" * 32, status=CommandStatus.UNKNOWN)

    with (
        pytest.raises(
            EngineerWorkItemCoordinatorError,
            match="verified_terminal_mismatch",
        ),
        storage.transaction() as conn,
    ):
        coordinator.settle_verified_terminal_in_transaction(
            conn,
            **scope,
            work_item_id=reserved.continuation.work_item_id,
            expected_revision=reserved.continuation.revision,
            verified_job_id="5" * 32,
            verified_terminal_receipt_sha256=TERMINAL,
            source=reservation.source,
            now=LATER,
        )
    unchanged = _item(storage, scope, reserved.continuation.work_item_id)
    assert unchanged.state is EngineerWorkItemState.ACTIVE


def test_structural_continuation_never_projects_external_private_material(storage: Any) -> None:
    conversation_id, scope = _scope(storage)
    ledger = _FakeCommandLedger()
    coordinator = EngineerWorkItemRuntimeCoordinator(ledger)
    reservation = _reservation(conversation_id)
    with storage.transaction() as conn:
        reserved = coordinator.reserve_initial_in_transaction(
            conn,
            reservation=reservation,
            now=NOW,
            expires_at=EXPIRY,
        )
    assert reserved.continuation is not None
    ledger.admit(reservation, job_id="6" * 32, status=CommandStatus.RUNNING)
    with storage.transaction() as conn:
        admitted = coordinator.reconcile_admission_in_transaction(
            conn,
            **scope,
            work_item_id=reserved.continuation.work_item_id,
            expected_revision=reserved.continuation.revision,
            source=reservation.source,
            now=LATER,
        )
        structural = coordinator.current_structural_state_in_transaction(conn, **scope)
    assert structural == admitted.continuation
    assert structural is not None
    projected = repr(asdict(structural))
    assert "private-command" not in projected
    assert "PRIVATE_STDOUT" not in projected
    assert "PRIVATE_STDERR" not in projected


def test_runtime_methods_require_a_caller_owned_main_transaction(storage: Any) -> None:
    conversation_id, _scope_value = _scope(storage)
    coordinator = EngineerWorkItemRuntimeCoordinator(_FakeCommandLedger())
    with pytest.raises(RuntimeError, match="existing transaction"):
        coordinator.reserve_initial_in_transaction(
            storage.conn,
            reservation=_reservation(conversation_id),
            now=NOW,
            expires_at=EXPIRY,
        )


def test_reservation_rejects_model_shaped_identity_before_any_storage_mutation(
    storage: Any,
) -> None:
    conversation_id, scope = _scope(storage)
    with pytest.raises(ValueError, match="canonical Engineer command key"):
        EngineerCommandReservation(
            source=_reservation(conversation_id).source,
            idempotency_key="please-retry-this-command",
            command_digest="c" * 64,
        )
    assert get_current_engineer_work_item_in_transaction(storage.conn, **scope) is None


def test_stale_revision_cannot_drive_reconciliation(storage: Any) -> None:
    conversation_id, scope = _scope(storage)
    ledger = _FakeCommandLedger()
    coordinator = EngineerWorkItemRuntimeCoordinator(ledger)
    reservation = _reservation(conversation_id)
    with storage.transaction() as conn:
        reserved = coordinator.reserve_initial_in_transaction(
            conn,
            reservation=reservation,
            now=NOW,
            expires_at=EXPIRY,
        )
    assert reserved.continuation is not None
    with pytest.raises(EngineerWorkItemConflictError), storage.transaction() as conn:
        coordinator.reconcile_admission_in_transaction(
            conn,
            **scope,
            work_item_id=reserved.continuation.work_item_id,
            expected_revision=reserved.continuation.revision + 1,
            now=LATER,
        )
