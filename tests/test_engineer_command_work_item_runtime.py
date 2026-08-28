from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from friday.engineer_source_binding import ENGINEER_SOURCE_MAX_CALL_ORDINAL
from friday.interaction_control_plane.engineer_work_item import (
    EngineerWorkItemChannel,
    EngineerWorkItemState,
    EngineerWorkItemStepState,
    get_current_engineer_work_item_in_transaction,
    get_engineer_work_item_in_transaction,
)
from friday.orchestration.engineer_work_item_coordinator import (
    EngineerCommandLedgerObservation,
    EngineerCommandReservation,
    EngineerCommandSourceSlot,
    EngineerWorkItemCoordinatorError,
    EngineerWorkItemRuntimeCoordinator,
)
from friday.organs.engineer import command_tools
from friday.organs.engineer.command import (
    CommandError,
    CommandProgress,
    CommandStatus,
    IsolationProfile,
)
from friday.organs.engineer.command_tools import EngineerCommandService
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext
from friday.storage._conversations import store_message_in_transaction

CHAT_ID = "5001"
UPDATE_ID = "7001"
COMMAND = "printf runtime-seam"
JOB_ID = "1" * 32


def _source_step_id(source_row_id: str, ordinal: int) -> str:
    material = (source_row_id + "\x00engineer-command-step\x00" + str(ordinal)).encode()
    return "ecstep-" + hashlib.sha256(material).hexdigest()[:32]


class _Ledger:
    def __init__(self) -> None:
        self.bindings: dict[tuple[str, str], dict[str, object]] = {}
        self.jobs: dict[str, dict[str, object]] = {}
        self.slots_by_source: dict[tuple[str, str], dict[str, object]] = {}
        self.slots_by_key: dict[tuple[str, str], dict[str, object]] = {}
        self.fences_by_source: dict[tuple[str, str], dict[str, object]] = {}
        self.fences_by_key: dict[tuple[str, str], dict[str, object]] = {}

    def assert_lifecycle_ready(self) -> None:
        return None

    def lookup_idempotency_binding(self, actor_id: str, key: str) -> dict[str, object] | None:
        value = self.bindings.get((actor_id, key))
        return dict(value) if value is not None else None

    def lookup_engineer_command_source_slot(
        self,
        actor_id: str,
        source_binding_sha256: str,
        *,
        legacy_source_binding_sha256: str | None = None,
    ) -> dict[str, object] | None:
        del legacy_source_binding_sha256
        value = self.slots_by_source.get((actor_id, source_binding_sha256))
        return dict(value) if value is not None else None

    def lookup_engineer_command_source_slot_by_key(
        self,
        actor_id: str,
        idempotency_key: str,
    ) -> dict[str, object] | None:
        value = self.slots_by_key.get((actor_id, idempotency_key))
        return dict(value) if value is not None else None

    def lookup_engineer_work_item_fence(
        self,
        actor_id: str,
        idempotency_key: str,
    ) -> dict[str, object] | None:
        value = self.fences_by_key.get((actor_id, idempotency_key))
        return dict(value) if value is not None else None

    def lookup_engineer_work_item_fence_by_source(
        self,
        actor_id: str,
        source_binding_sha256: str,
    ) -> dict[str, object] | None:
        value = self.fences_by_source.get((actor_id, source_binding_sha256))
        return dict(value) if value is not None else None

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
        del legacy_source_binding_sha256, created_at
        fence: dict[str, object] = {
            "actor_id": actor_id,
            "work_item_id": work_item_id,
            "expected_revision": expected_revision,
            "step_ordinal": step_ordinal,
            "source_binding_sha256": source_binding_sha256,
            "idempotency_key": idempotency_key,
            "command_digest": command_digest,
        }
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
        self.fences_by_key[(actor_id, idempotency_key)] = fence
        self.fences_by_source[(actor_id, source_binding_sha256)] = fence
        self.slots_by_key[(actor_id, idempotency_key)] = slot
        self.slots_by_source[(actor_id, source_binding_sha256)] = slot
        return dict(fence)

    def read_job(self, job_id: str) -> dict[str, object]:
        return dict(self.jobs[job_id])

    def admit(
        self,
        reservation: EngineerCommandReservation,
        *,
        status: CommandStatus = CommandStatus.RUNNING,
    ) -> None:
        source = reservation.source
        binding: dict[str, object] = {
            "job_id": JOB_ID,
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
        slot: dict[str, object] = {
            "actor_id": source.owner_id,
            "source_binding_sha256": source.binding_sha256(),
            "legacy_source_binding_sha256": None,
            "idempotency_key": reservation.idempotency_key,
            "command_digest": reservation.command_digest,
            "target_kind": "job",
            "job_id": JOB_ID,
            "fence_actor_id": None,
            "fence_idempotency_key": None,
            "work_item_id": None,
            "expected_revision": None,
            "step_ordinal": None,
        }
        self.bindings[(source.owner_id, reservation.idempotency_key)] = binding
        self.jobs[JOB_ID] = {
            **{key: value for key, value in binding.items() if key != "delivery_chat_id"},
            "status": status.value,
        }
        self.slots_by_source[(source.owner_id, source.binding_sha256())] = slot
        self.slots_by_key[(source.owner_id, reservation.idempotency_key)] = slot

    def lose_job_authority(self) -> None:
        self.bindings.clear()
        self.jobs.clear()
        self.slots_by_source.clear()
        self.slots_by_key.clear()


class _SourceAuthority:
    def __init__(self) -> None:
        self.last: dict[str, object] | None = None

    def attest(self, **arguments: object) -> SimpleNamespace:
        self.last = dict(arguments)
        return SimpleNamespace(**arguments)

    def delegate_autonomous(self, _source: object, *, expires_at: int) -> str:
        assert expires_at > 0
        return "delegation"


class _Authority:
    def __init__(self) -> None:
        self.source_authority = _SourceAuthority()
        self.issue_calls = 0

    def issue_autonomous(self, *_args: object, **_kwargs: object) -> str:
        self.issue_calls += 1
        return "grant"


class _Kernel:
    def __init__(self, ledger: _Ledger) -> None:
        self.ledger = ledger
        self.authority = _Authority()
        self.submit_calls = 0

    def submit(
        self,
        request: Any,
        _grant: str,
        *,
        actor_id: str,
        delivery_chat_id: str,
        **_kwargs: object,
    ) -> str:
        self.submit_calls += 1
        source = self.authority.source_authority.last
        assert source is not None
        reservation = EngineerCommandReservation(
            source=EngineerCommandSourceSlot(
                owner_id=actor_id,
                tenant_id=str(source["tenant_id"]),
                conversation_id=str(source["conversation_id"]),
                channel=EngineerWorkItemChannel(str(source["channel"])),
                source_row_id=str(source["source_row_id"]),
                source_step_id=str(source["source_step_id"]),
                source_hash=str(source["source_hash"]),
                telegram_update_id=str(source["telegram_update_id"]),
                delivery_chat_id=delivery_chat_id,
            ),
            idempotency_key=request.idempotency_key,
            command_digest=request.digest,
        )
        self.ledger.admit(reservation)
        return JOB_ID

    def wait(self, *_args: object, **_kwargs: object) -> None:
        raise CommandError("wait_timeout")

    def resolve_job_reference(
        self,
        job_id: str | None,
        *,
        actor_id: str,
        tenant_id: str,
        conversation_id: str,
        channel: str,
        operation: str = "status",
    ) -> str:
        assert actor_id == tenant_id == LEGACY_OWNER_USER_ID
        assert conversation_id
        assert channel == EngineerWorkItemChannel.TELEGRAM.value
        assert operation in {"status", "cancel"}
        resolved = str(job_id or JOB_ID)
        assert resolved in self.ledger.jobs
        return resolved

    def cancel_reference(
        self,
        job_id: str | None,
        *,
        actor_id: str,
        tenant_id: str,
        conversation_id: str,
        channel: str,
    ) -> str:
        resolved = self.resolve_job_reference(
            job_id,
            actor_id=actor_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            channel=channel,
            operation="cancel",
        )
        self.ledger.jobs[resolved]["status"] = CommandStatus.CANCELLED.value
        return resolved

    def progress(
        self,
        job_id: str,
        *,
        actor_id: str,
        conversation_id: str | None = None,
    ) -> CommandProgress:
        assert (job_id, actor_id) == (JOB_ID, LEGACY_OWNER_USER_ID)
        assert conversation_id is not None
        status = CommandStatus(str(self.ledger.jobs.get(job_id, {}).get("status") or "running"))
        return CommandProgress(
            job_id=job_id,
            status=status,
            elapsed_sec=1.0,
            stdout_bytes=0,
            stderr_bytes=0,
            output_activity=False,
            isolation_profile=IsolationProfile.HOST_USER,
        )


@dataclass
class _Seam:
    service: EngineerCommandService
    actor: ActorContext
    conversation_id: str
    source_id: str
    step_id: str
    source: EngineerCommandSourceSlot
    ledger: _Ledger
    kernel: _Kernel


@pytest.fixture
def seam(storage: Any) -> _Seam:
    actor = ActorContext(
        LEGACY_OWNER_USER_ID,
        "owner",
        "telegram-bridge",
        identity_id=CHAT_ID,
        telegram_chat_id=CHAT_ID,
    )
    storage.ensure_user(actor.own_id, preset_key="owner", metadata={"chat_id": CHAT_ID})
    conversation = storage.create_conversation(actor.user_id, title="EWI command seam")
    source = storage.store_message(
        str(conversation["id"]),
        actor.own_id,
        "user",
        "Запусти проверку runtime seam",
        metadata={"conversation_uploaded_raw_ids": [], "telegram_update_id": UPDATE_ID},
    )
    step_id = _source_step_id(str(source["id"]), 1)
    command_source = EngineerCommandSourceSlot(
        owner_id=actor.own_id,
        tenant_id=actor.user_id,
        conversation_id=str(conversation["id"]),
        channel=EngineerWorkItemChannel.TELEGRAM,
        source_row_id=str(source["id"]),
        source_step_id=step_id,
        source_hash=hashlib.sha256(str(source["content"]).encode()).hexdigest(),
        telegram_update_id=UPDATE_ID,
        delivery_chat_id=CHAT_ID,
    )
    ledger = _Ledger()
    kernel = _Kernel(ledger)
    service = EngineerCommandService.__new__(EngineerCommandService)
    service.storage = storage
    service.kernel = kernel  # type: ignore[assignment]
    service.work_items = EngineerWorkItemRuntimeCoordinator(ledger)
    service.files_root = Path("/not-used-without-uploads")
    service.max_upload_bytes = 1024 * 1024
    service._fresh_owner_actor = lambda _actor, _capability: actor  # type: ignore[method-assign]
    return _Seam(
        service=service,
        actor=actor,
        conversation_id=str(conversation["id"]),
        source_id=str(source["id"]),
        step_id=step_id,
        source=command_source,
        ledger=ledger,
        kernel=kernel,
    )


def _execute(seam: _Seam) -> dict[str, Any]:
    return seam.service.execute(
        actor=seam.actor,
        command=COMMAND,
        timeout_sec=10,
        _conversation_id=seam.conversation_id,
        _source_message_id=seam.source_id,
        _telegram_update_id=UPDATE_ID,
        _step_id=seam.step_id,
    )


def _reservation(seam: _Seam) -> EngineerCommandReservation:
    preliminary = command_tools._command_request(  # noqa: SLF001
        command=COMMAND,
        timeout_sec=10,
        idempotency_key="pending",
        input_manifest=command_tools.EMPTY_INPUT_MANIFEST,
    )
    return EngineerCommandReservation(
        source=seam.source,
        idempotency_key=command_tools._idempotency_key(  # noqa: SLF001
            seam.source_id,
            seam.step_id,
            preliminary,
        ),
        command_digest=preliminary.digest,
    )


def _leave_prepared(seam: _Seam) -> EngineerCommandReservation:
    reservation = _reservation(seam)
    with seam.service.storage.transaction() as conn:
        outcome = seam.service.work_items.reserve_initial_in_transaction(
            conn,
            reservation=reservation,
        )
    assert outcome.can_submit
    assert outcome.continuation is not None
    assert outcome.continuation.step_state is EngineerWorkItemStepState.PREPARED
    return reservation


def test_execute_admits_once_and_exact_replay_never_submits_again(seam: _Seam) -> None:
    first = _execute(seam)
    replay = _execute(seam)

    assert first["ok"] is replay["ok"] is True
    assert first["job_id"] == replay["job_id"] == JOB_ID
    assert first["status"] == replay["status"] == CommandStatus.RUNNING.value
    assert seam.kernel.submit_calls == 1
    assert seam.kernel.authority.issue_calls == 1


def test_resume_prepared_absent_recovers_local_source_then_fences_and_retires(
    seam: _Seam,
) -> None:
    reservation = _leave_prepared(seam)

    resumed = seam.service.resume_current(
        actor=seam.actor,
        conversation_id=seam.conversation_id,
    )

    assert resumed is None
    assert seam.kernel.submit_calls == 0
    assert (
        seam.ledger.lookup_engineer_work_item_fence(
            seam.actor.own_id,
            reservation.idempotency_key,
        )
        is not None
    )
    with seam.service.storage.transaction() as conn:
        assert (
            get_current_engineer_work_item_in_transaction(
                conn,
                owner_id=seam.actor.own_id,
                tenant_id=seam.actor.user_id,
                conversation_id=seam.conversation_id,
                channel=EngineerWorkItemChannel.TELEGRAM,
            )
            is None
        )


def test_prepared_source_older_than_one_thousand_later_authenticated_messages_recovers_and_fences(
    seam: _Seam,
) -> None:
    reservation = _leave_prepared(seam)
    later_count = 1001
    with seam.service.storage.transaction() as conn:
        for index in range(later_count):
            store_message_in_transaction(
                conn,
                seam.conversation_id,
                seam.actor.own_id,
                "user",
                f"Позднее аутентифицированное сообщение {index}",
                metadata={
                    "conversation_uploaded_raw_ids": [],
                    "telegram_update_id": str(800_000 + index),
                },
            )
        later_rows = conn.execute(
            """SELECT COUNT(*) FROM messages
                 WHERE conversation_id=? AND user_id=? AND rowid>(
                     SELECT rowid FROM messages WHERE id=? AND user_id=?
                 )""",
            (
                seam.conversation_id,
                seam.actor.own_id,
                seam.source_id,
                seam.actor.own_id,
            ),
        ).fetchone()[0]
    assert later_rows == later_count

    resumed = seam.service.resume_current(
        actor=seam.actor,
        conversation_id=seam.conversation_id,
    )

    assert resumed is None
    fence = seam.ledger.lookup_engineer_work_item_fence(
        seam.actor.own_id,
        reservation.idempotency_key,
    )
    assert fence is not None
    assert fence["source_binding_sha256"] == seam.source.binding_sha256()
    with seam.service.storage.transaction() as conn:
        assert (
            get_current_engineer_work_item_in_transaction(
                conn,
                owner_id=seam.actor.own_id,
                tenant_id=seam.actor.user_id,
                conversation_id=seam.conversation_id,
                channel=EngineerWorkItemChannel.TELEGRAM,
            )
            is None
        )


def test_prepared_source_at_shared_maximum_call_ordinal_recovers_and_fences(
    seam: _Seam,
) -> None:
    seam.step_id = _source_step_id(
        seam.source_id,
        ENGINEER_SOURCE_MAX_CALL_ORDINAL,
    )
    seam.source = replace(seam.source, source_step_id=seam.step_id)
    reservation = _leave_prepared(seam)

    resumed = seam.service.resume_current(
        actor=seam.actor,
        conversation_id=seam.conversation_id,
    )

    assert resumed is None
    fence = seam.ledger.lookup_engineer_work_item_fence(
        seam.actor.own_id,
        reservation.idempotency_key,
    )
    assert fence is not None
    assert fence["source_binding_sha256"] == seam.source.binding_sha256()
    with seam.service.storage.transaction() as conn:
        assert (
            get_current_engineer_work_item_in_transaction(
                conn,
                owner_id=seam.actor.own_id,
                tenant_id=seam.actor.user_id,
                conversation_id=seam.conversation_id,
                channel=EngineerWorkItemChannel.TELEGRAM,
            )
            is None
        )


def test_resume_prepared_exact_binds_existing_job_without_submit(seam: _Seam) -> None:
    reservation = _leave_prepared(seam)
    seam.ledger.admit(reservation)

    resumed = seam.service.resume_current(
        actor=seam.actor,
        conversation_id=seam.conversation_id,
    )

    assert resumed is not None
    assert resumed.payload["job_id"] == JOB_ID
    assert resumed.payload["status"] == CommandStatus.RUNNING.value
    assert resumed.continuation.step_state is EngineerWorkItemStepState.ADMITTED
    assert seam.kernel.submit_calls == 0


@pytest.mark.parametrize(
    ("operation", "expected_status"),
    [
        ("status", CommandStatus.RUNNING),
        ("cancel", CommandStatus.CANCELLED),
    ],
)
def test_historical_status_and_cancel_emit_exact_private_ledger_observation(
    seam: _Seam,
    operation: str,
    expected_status: CommandStatus,
) -> None:
    reservation = _reservation(seam)
    seam.ledger.admit(reservation)

    result = getattr(seam.service, operation)(
        actor=seam.actor,
        job_id=JOB_ID,
        _conversation_id=seam.conversation_id,
    )

    observation = result.get("_engineer_command_ledger_observation")
    assert type(observation) is EngineerCommandLedgerObservation
    assert observation == EngineerCommandLedgerObservation(
        owner_id=seam.actor.own_id,
        tenant_id=seam.actor.user_id,
        conversation_id=seam.conversation_id,
        job_id=JOB_ID,
        status=expected_status,
    )
    assert result["job_id"] == JOB_ID
    assert result["status"] == expected_status.value
    assert (result.get("cancel_requested") is True) is (operation == "cancel")


def test_known_job_ledger_loss_fails_closed_without_replay_or_retirement(seam: _Seam) -> None:
    admitted = _execute(seam)
    assert admitted["job_id"] == JOB_ID
    seam.ledger.lose_job_authority()

    replay = _execute(seam)

    assert replay == {
        "effect_boundary_crossed": False,
        "error_code": "command_ledger_lost",
        "ok": False,
        "status": "failed",
    }
    assert seam.kernel.submit_calls == 1
    assert seam.ledger.fences_by_key == {}
    with pytest.raises(EngineerWorkItemCoordinatorError, match="command_ledger_lost"):
        seam.service.resume_current(
            actor=seam.actor,
            conversation_id=seam.conversation_id,
        )
    with seam.service.storage.transaction() as conn:
        current = get_current_engineer_work_item_in_transaction(
            conn,
            owner_id=seam.actor.own_id,
            tenant_id=seam.actor.user_id,
            conversation_id=seam.conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
        )
    assert current is not None
    assert current.current_step.state is EngineerWorkItemStepState.ADMITTED


def test_resume_lazily_expires_stale_settled_item_and_releases_scope(seam: _Seam) -> None:
    reservation = _reservation(seam)
    with seam.service.storage.transaction() as conn:
        prepared = seam.service.work_items.reserve_initial_in_transaction(
            conn,
            reservation=reservation,
            now="2020-01-01T00:00:00+00:00",
            expires_at="2020-01-01T01:00:00+00:00",
        )
    assert prepared.continuation is not None
    seam.ledger.admit(reservation, status=CommandStatus.COMPLETED)
    with seam.service.storage.transaction() as conn:
        admitted = seam.service.work_items.reconcile_admission_in_transaction(
            conn,
            work_item_id=prepared.continuation.work_item_id,
            owner_id=seam.actor.own_id,
            tenant_id=seam.actor.user_id,
            conversation_id=seam.conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
            expected_revision=prepared.continuation.revision,
            source=reservation.source,
            now="2020-01-01T00:00:01+00:00",
        )
        assert admitted.continuation is not None
        settled = seam.service.work_items.settle_verified_terminal_in_transaction(
            conn,
            work_item_id=admitted.continuation.work_item_id,
            owner_id=seam.actor.own_id,
            tenant_id=seam.actor.user_id,
            conversation_id=seam.conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
            expected_revision=admitted.continuation.revision,
            verified_job_id=JOB_ID,
            verified_terminal_receipt_sha256="f" * 64,
            source=reservation.source,
            now="2020-01-01T00:00:02+00:00",
        )

    assert seam.service.resume_current(actor=seam.actor, conversation_id=seam.conversation_id) is None
    with seam.service.storage.transaction() as conn:
        current = get_current_engineer_work_item_in_transaction(
            conn,
            owner_id=seam.actor.own_id,
            tenant_id=seam.actor.user_id,
            conversation_id=seam.conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
        )
        expired = get_engineer_work_item_in_transaction(
            conn,
            work_item_id=settled.work_item_id,
            owner_id=seam.actor.own_id,
            tenant_id=seam.actor.user_id,
            conversation_id=seam.conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
        )
    assert current is None
    assert expired is not None and expired.state is EngineerWorkItemState.EXPIRED
