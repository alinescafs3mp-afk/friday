"""Runtime coordination for body-free, restart-safe Engineer command work.

The main Friday database owns continuation state.  The independent command
ledger owns command admission and terminal receipts.  This module is the small
cross-database seam between them: it never stores a command, argv, path, model
text, stdout, stderr, or generated-file inventory in the main database.

There is deliberately no distributed transaction.  Every external read is
treated as authority, every uncertainty fails closed, and an admitted or
UNKNOWN command is observed rather than replayed.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar

from friday.engineer_source_binding import legacy_engineer_source_binding_sha256
from friday.interaction_control_plane.engineer_work_item import (
    EngineerWorkItem,
    EngineerWorkItemChannel,
    EngineerWorkItemConflictError,
    EngineerWorkItemState,
    EngineerWorkItemStepState,
    EngineerWorkItemTransition,
    bind_engineer_command_receipts_in_transaction,
    close_engineer_work_item_in_transaction,
    create_engineer_work_item_in_transaction,
    discard_unsubmitted_engineer_work_item_in_transaction,
    engineer_job_receipt_sha256,
    engineer_source_binding_sha256,
    get_current_engineer_work_item_in_transaction,
    get_engineer_work_item_in_transaction,
    mark_engineer_command_unknown_in_transaction,
    mark_engineer_work_item_ready_to_answer_in_transaction,
    rollback_fenced_unsubmitted_engineer_step_in_transaction,
    settle_engineer_terminal_receipt_in_transaction,
    start_next_engineer_step_in_transaction,
)
from friday.interaction_control_plane.engineer_work_item_schema import (
    ENGINEER_WORK_ITEM_COMPLETION_CONTRACT_SHA256,
)
from friday.organs.engineer.command.contracts import CommandStatus

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_IDEMPOTENCY_KEY_RE = re.compile(r"ecmd-[0-9a-f]{64}\Z")
_JOB_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_LEDGER_BINDING_KEYS = frozenset(
    {
        "job_id",
        "actor_id",
        "tenant_id",
        "conversation_id",
        "channel",
        "source_row_id",
        "source_step_id",
        "source_hash",
        "telegram_update_id",
        "idempotency_key",
        "command_digest",
        "delivery_chat_id",
    }
)
_FENCE_KEYS = frozenset(
    {
        "actor_id",
        "work_item_id",
        "expected_revision",
        "step_ordinal",
        "source_binding_sha256",
        "idempotency_key",
        "command_digest",
    }
)
_SOURCE_SLOT_KEYS = frozenset(
    {
        "actor_id",
        "source_binding_sha256",
        "legacy_source_binding_sha256",
        "idempotency_key",
        "command_digest",
        "target_kind",
        "job_id",
        "fence_actor_id",
        "fence_idempotency_key",
        "work_item_id",
        "expected_revision",
        "step_ordinal",
    }
)
_PUBLISHABLE_TERMINAL = frozenset(
    {
        CommandStatus.COMPLETED,
        CommandStatus.FAILED,
        CommandStatus.CANCELLED,
        CommandStatus.TIMEOUT,
    }
)


class EngineerWorkItemCoordinatorError(RuntimeError):
    """Stable fail-closed runtime coordination error."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "engineer_continuation_failed")[:80]
        super().__init__(self.code)


class EngineerCommandLedgerDisposition(StrEnum):
    ABSENT = "absent"
    EXACT = "exact"
    FENCED = "fenced"


class EngineerCommandLedger(Protocol):
    """The body-free CommandJobStore surface used by the coordinator."""

    def assert_lifecycle_ready(self) -> None: ...

    def lookup_idempotency_binding(
        self,
        actor_id: str,
        key: str,
    ) -> Mapping[str, object] | None: ...

    def lookup_engineer_command_source_slot(
        self,
        actor_id: str,
        source_binding_sha256: str,
        *,
        legacy_source_binding_sha256: str | None = None,
    ) -> Mapping[str, object] | None: ...

    def lookup_engineer_command_source_slot_by_key(
        self,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, object] | None: ...

    def lookup_engineer_work_item_fence(
        self,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, object] | None: ...

    def lookup_engineer_work_item_fence_by_source(
        self,
        actor_id: str,
        source_binding_sha256: str,
    ) -> Mapping[str, object] | None: ...

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
    ) -> Mapping[str, object]: ...

    def read_job(self, job_id: str) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class EngineerCommandSourceSlot:
    """One exact authenticated Telegram call slot; never message content."""

    owner_id: str
    tenant_id: str
    conversation_id: str
    channel: EngineerWorkItemChannel
    source_row_id: str
    source_step_id: str
    source_hash: str
    telegram_update_id: str
    delivery_chat_id: str

    def __post_init__(self) -> None:
        # Reuse the durable contract as the sole source identity validator.
        self.binding_sha256()

    def binding_sha256(self) -> str:
        return engineer_source_binding_sha256(
            owner_id=self.owner_id,
            tenant_id=self.tenant_id,
            conversation_id=self.conversation_id,
            channel=self.channel,
            source_row_id=self.source_row_id,
            source_step_id=self.source_step_id,
            source_hash=self.source_hash,
            telegram_update_id=self.telegram_update_id,
            delivery_chat_id=self.delivery_chat_id,
        )

    def legacy_binding_sha256(self) -> str:
        """Return only the conservative pre-slot alias used for collision lookup."""

        return legacy_engineer_source_binding_sha256(
            owner_id=self.owner_id,
            tenant_id=self.tenant_id,
            conversation_id=self.conversation_id,
            channel=self.channel.value,
            source_row_id=self.source_row_id,
            source_hash=self.source_hash,
            telegram_update_id=self.telegram_update_id,
            delivery_chat_id=self.delivery_chat_id,
        )


@dataclass(frozen=True, slots=True)
class EngineerCommandReservation:
    """Body-free identity of one command the runtime is preparing to submit."""

    source: EngineerCommandSourceSlot
    idempotency_key: str
    command_digest: str

    def __post_init__(self) -> None:
        if _IDEMPOTENCY_KEY_RE.fullmatch(self.idempotency_key) is None:
            raise ValueError("idempotency_key is not a canonical Engineer command key")
        if _DIGEST_RE.fullmatch(self.command_digest) is None:
            raise ValueError("command_digest is not a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class EngineerContinuationState:
    """Compact structural state safe to inject into a continuation decision."""

    work_item_id: str
    owner_id: str
    tenant_id: str
    conversation_id: str
    channel: EngineerWorkItemChannel
    state: EngineerWorkItemState
    transition: EngineerWorkItemTransition
    revision: int
    step_ordinal: int
    step_state: EngineerWorkItemStepState
    source_binding_sha256: str
    idempotency_key: str
    command_digest: str
    job_receipt_sha256: str
    terminal_receipt_sha256: str
    ledger_disposition: EngineerCommandLedgerDisposition
    command_job_id: str | None
    command_status: CommandStatus | None

    @property
    def can_submit(self) -> bool:
        """True only after an exact healthy negative command-ledger lookup."""

        return (
            self.state is EngineerWorkItemState.ACTIVE
            and self.step_state is EngineerWorkItemStepState.PREPARED
            and self.ledger_disposition is EngineerCommandLedgerDisposition.ABSENT
            and self.command_job_id is None
            and self.command_status is None
        )


@dataclass(frozen=True, slots=True)
class EngineerCommandLedgerObservation:
    """Private exact scope/status for a command outside an open Work Item."""

    owner_id: str
    tenant_id: str
    conversation_id: str
    job_id: str
    status: CommandStatus

    def __post_init__(self) -> None:
        for value in (self.owner_id, self.tenant_id, self.conversation_id):
            if not isinstance(value, str) or not value or len(value) > 128 or "\x00" in value:
                raise ValueError("Engineer ledger observation scope is invalid")
        if _JOB_ID_RE.fullmatch(self.job_id) is None or type(self.status) is not CommandStatus:
            raise ValueError("Engineer ledger observation identity is invalid")


@dataclass(frozen=True, slots=True)
class EngineerAdmissionOutcome:
    """Result of reserving/reconciling one exact command identity."""

    disposition: EngineerCommandLedgerDisposition
    continuation: EngineerContinuationState | None

    @property
    def can_submit(self) -> bool:
        return self.continuation is not None and self.continuation.can_submit


@dataclass(frozen=True, slots=True)
class _ExactLedgerBinding:
    raw: Mapping[str, object]
    job_id: str
    job_receipt_sha256: str
    status: CommandStatus


@dataclass(frozen=True, slots=True)
class _ExactSourceSlot:
    raw: Mapping[str, object]
    target_kind: str
    job_id: str | None


@dataclass(frozen=True, slots=True)
class _LedgerObservation:
    disposition: EngineerCommandLedgerDisposition
    binding: _ExactLedgerBinding | None = None
    fence: Mapping[str, object] | None = None
    source_slot: _ExactSourceSlot | None = None


_T = TypeVar("_T")


class EngineerWorkItemRuntimeCoordinator:
    """Coordinate one durable main-DB Work Item with CommandJobStore truth."""

    def __init__(self, command_ledger: EngineerCommandLedger) -> None:
        self._ledger = command_ledger

    @staticmethod
    def _require_transaction(conn: sqlite3.Connection) -> None:
        if not conn.in_transaction:
            raise RuntimeError("Engineer Work Item coordination requires an existing transaction")

    @staticmethod
    @contextmanager
    def _coordination(conn: sqlite3.Connection) -> Iterator[None]:
        """Rollback a whole compound coordination call even if its caller catches."""

        EngineerWorkItemRuntimeCoordinator._require_transaction(conn)
        savepoint = f"engineer_work_item_coordinator_{uuid.uuid4().hex}"
        conn.execute(f'SAVEPOINT "{savepoint}"')  # nosec B608 - generated hexadecimal name
        try:
            yield
        except BaseException:
            conn.execute(f'ROLLBACK TO SAVEPOINT "{savepoint}"')  # nosec B608
            conn.execute(f'RELEASE SAVEPOINT "{savepoint}"')  # nosec B608
            raise
        conn.execute(f'RELEASE SAVEPOINT "{savepoint}"')  # nosec B608

    @staticmethod
    def _ledger_call(
        callback: Callable[..., _T],
        *args: object,
        **kwargs: object,
    ) -> _T:
        try:
            return callback(*args, **kwargs)
        except EngineerWorkItemCoordinatorError:
            raise
        except Exception as exc:
            raise EngineerWorkItemCoordinatorError("command_ledger_unavailable") from exc

    @staticmethod
    def _required_revision(item: EngineerWorkItem, expected_revision: int) -> None:
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or item.revision != expected_revision
        ):
            raise EngineerWorkItemConflictError("Engineer Work Item revision is no longer current")

    def _required_item(
        self,
        conn: sqlite3.Connection,
        *,
        work_item_id: str,
        owner_id: str,
        tenant_id: str,
        conversation_id: str,
        channel: EngineerWorkItemChannel,
        expected_revision: int,
    ) -> EngineerWorkItem:
        item = get_engineer_work_item_in_transaction(
            conn,
            work_item_id=work_item_id,
            owner_id=owner_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            channel=channel,
        )
        if item is None:
            raise EngineerWorkItemConflictError("Engineer Work Item is unavailable")
        self._required_revision(item, expected_revision)
        return item

    @staticmethod
    def _validated_source_for_item(
        item: EngineerWorkItem,
        source: EngineerCommandSourceSlot,
    ) -> EngineerCommandSourceSlot:
        if (
            source.owner_id != item.owner_id
            or source.tenant_id != item.tenant_id
            or source.conversation_id != item.conversation_id
            or source.channel is not item.channel
            or source.binding_sha256() != item.current_step.source_binding_sha256
        ):
            raise EngineerWorkItemCoordinatorError("command_source_mismatch")
        return source

    @staticmethod
    def _validate_source_slot(
        item: EngineerWorkItem,
        raw: Mapping[str, object],
    ) -> _ExactSourceSlot:
        if not isinstance(raw, Mapping) or frozenset(raw) != _SOURCE_SLOT_KEYS:
            raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
        text_keys = {
            "actor_id",
            "source_binding_sha256",
            "idempotency_key",
            "command_digest",
            "target_kind",
        }
        if any(type(raw[key]) is not str for key in text_keys):
            raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
        legacy = raw["legacy_source_binding_sha256"]
        if legacy is not None and (type(legacy) is not str or _DIGEST_RE.fullmatch(legacy) is None):
            raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
        if (
            raw["actor_id"] != item.owner_id
            or raw["source_binding_sha256"] != item.current_step.source_binding_sha256
            or legacy is not None
            or raw["idempotency_key"] != item.current_step.idempotency_key
            or raw["command_digest"] != item.current_step.command_digest
        ):
            raise EngineerWorkItemCoordinatorError("command_source_slot_conflict")
        target_kind = str(raw["target_kind"])
        if target_kind == "job":
            job_id = raw["job_id"]
            if (
                type(job_id) is not str
                or _JOB_ID_RE.fullmatch(job_id) is None
                or any(
                    raw[key] is not None
                    for key in (
                        "fence_actor_id",
                        "fence_idempotency_key",
                        "work_item_id",
                        "expected_revision",
                        "step_ordinal",
                    )
                )
            ):
                raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
            return _ExactSourceSlot(raw=dict(raw), target_kind=target_kind, job_id=job_id)
        if target_kind == "engineer_work_item_fence":
            if (
                raw["job_id"] is not None
                or raw["fence_actor_id"] != item.owner_id
                or raw["fence_idempotency_key"] != item.current_step.idempotency_key
                or raw["work_item_id"] != item.id
                or type(raw["expected_revision"]) is not int
                or raw["expected_revision"] != item.revision
                or type(raw["step_ordinal"]) is not int
                or raw["step_ordinal"] != item.step_ordinal
            ):
                raise EngineerWorkItemCoordinatorError("command_source_slot_conflict")
            return _ExactSourceSlot(raw=dict(raw), target_kind=target_kind, job_id=None)
        raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")

    @staticmethod
    def _validate_fence(
        item: EngineerWorkItem,
        raw: Mapping[str, object],
    ) -> Mapping[str, object]:
        if not isinstance(raw, Mapping) or frozenset(raw) != _FENCE_KEYS:
            raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
        if (
            any(type(raw[key]) is not str for key in _FENCE_KEYS - {"expected_revision", "step_ordinal"})
            or type(raw["expected_revision"]) is not int
            or type(raw["step_ordinal"]) is not int
        ):
            raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
        expected: dict[str, object] = {
            "actor_id": item.owner_id,
            "work_item_id": item.id,
            "expected_revision": item.revision,
            "step_ordinal": item.step_ordinal,
            "source_binding_sha256": item.current_step.source_binding_sha256,
            "idempotency_key": item.current_step.idempotency_key,
            "command_digest": item.current_step.command_digest,
        }
        if dict(raw) != expected:
            raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
        return expected

    @staticmethod
    def _validate_job_row(
        row: Mapping[str, object],
        binding: Mapping[str, object],
    ) -> CommandStatus:
        if not isinstance(row, Mapping):
            raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
        for key in _LEDGER_BINDING_KEYS - {"delivery_chat_id"}:
            if key not in row or row[key] != binding[key]:
                raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
        try:
            raw_status = row["status"]
            if type(raw_status) is not str:
                raise TypeError("status is not text")
            status = CommandStatus(raw_status)
        except (KeyError, TypeError, ValueError) as exc:
            raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent") from exc
        return status

    def _validate_binding(
        self,
        item: EngineerWorkItem,
        raw: Mapping[str, object],
    ) -> _ExactLedgerBinding:
        try:
            if not isinstance(raw, Mapping) or frozenset(raw) != _LEDGER_BINDING_KEYS:
                raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
            if any(type(raw[key]) is not str for key in _LEDGER_BINDING_KEYS):
                raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
            channel = EngineerWorkItemChannel(str(raw["channel"]))
            source = engineer_source_binding_sha256(
                owner_id=str(raw["actor_id"]),
                tenant_id=str(raw["tenant_id"]),
                conversation_id=str(raw["conversation_id"]),
                channel=channel,
                source_row_id=str(raw["source_row_id"]),
                source_step_id=str(raw["source_step_id"]),
                source_hash=str(raw["source_hash"]),
                telegram_update_id=str(raw["telegram_update_id"]),
                delivery_chat_id=str(raw["delivery_chat_id"]),
            )
            job_id = str(raw["job_id"])
            if _JOB_ID_RE.fullmatch(job_id) is None:
                raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
            if (
                raw["actor_id"] != item.owner_id
                or raw["tenant_id"] != item.tenant_id
                or raw["conversation_id"] != item.conversation_id
                or channel is not item.channel
                or source != item.current_step.source_binding_sha256
                or raw["idempotency_key"] != item.current_step.idempotency_key
                or raw["command_digest"] != item.current_step.command_digest
            ):
                raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
            receipt = engineer_job_receipt_sha256(
                owner_id=item.owner_id,
                tenant_id=item.tenant_id,
                conversation_id=item.conversation_id,
                channel=item.channel,
                source_binding_sha256=source,
                delivery_chat_id=str(raw["delivery_chat_id"]),
                idempotency_key=str(raw["idempotency_key"]),
                command_digest=str(raw["command_digest"]),
                job_id=job_id,
            )
            if item.current_step.job_receipt_sha256 not in {"", receipt}:
                raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
            job = self._ledger_call(self._ledger.read_job, job_id)
            status = self._validate_job_row(job, raw)
            return _ExactLedgerBinding(
                raw=dict(raw),
                job_id=job_id,
                job_receipt_sha256=receipt,
                status=status,
            )
        except EngineerWorkItemCoordinatorError:
            raise
        except Exception as exc:
            raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent") from exc

    def _observe(
        self,
        item: EngineerWorkItem,
        *,
        source: EngineerCommandSourceSlot | None = None,
    ) -> _LedgerObservation:
        self._ledger_call(self._ledger.assert_lifecycle_ready)
        legacy_binding: str | None = None
        if source is not None:
            source = self._validated_source_for_item(item, source)
            legacy_binding = source.legacy_binding_sha256()
        slot_by_source = self._ledger_call(
            self._ledger.lookup_engineer_command_source_slot,
            item.owner_id,
            item.current_step.source_binding_sha256,
            legacy_source_binding_sha256=legacy_binding,
        )
        slot_by_key = self._ledger_call(
            self._ledger.lookup_engineer_command_source_slot_by_key,
            item.owner_id,
            item.current_step.idempotency_key,
        )
        binding_raw = self._ledger_call(
            self._ledger.lookup_idempotency_binding,
            item.owner_id,
            item.current_step.idempotency_key,
        )
        fence_by_key = self._ledger_call(
            self._ledger.lookup_engineer_work_item_fence,
            item.owner_id,
            item.current_step.idempotency_key,
        )
        fence_by_source = self._ledger_call(
            self._ledger.lookup_engineer_work_item_fence_by_source,
            item.owner_id,
            item.current_step.source_binding_sha256,
        )

        if slot_by_source is None and slot_by_key is None:
            if binding_raw is not None or fence_by_key is not None or fence_by_source is not None:
                raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
            if item.current_step.state is not EngineerWorkItemStepState.PREPARED:
                raise EngineerWorkItemCoordinatorError("command_ledger_lost")
            if source is None:
                raise EngineerWorkItemCoordinatorError("command_source_required")
            return _LedgerObservation(disposition=EngineerCommandLedgerDisposition.ABSENT)

        if (
            slot_by_source is not None
            and legacy_binding is not None
            and slot_by_source.get("source_binding_sha256") == legacy_binding
            and slot_by_source.get("legacy_source_binding_sha256") == legacy_binding
        ):
            raise EngineerWorkItemCoordinatorError("legacy_command_source_collision")
        if slot_by_source is None or slot_by_key is None or dict(slot_by_source) != dict(slot_by_key):
            raise EngineerWorkItemCoordinatorError("command_source_slot_conflict")
        source_slot = self._validate_source_slot(item, slot_by_source)

        if source_slot.target_kind == "job":
            if binding_raw is None:
                raise EngineerWorkItemCoordinatorError("command_ledger_lost")
            if fence_by_key is not None or fence_by_source is not None:
                raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
            binding = self._validate_binding(item, binding_raw)
            if binding.job_id != source_slot.job_id:
                raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
            if (
                item.current_step.state is not EngineerWorkItemStepState.PREPARED
                and not item.current_step.job_receipt_sha256
            ):
                raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
            return _LedgerObservation(
                disposition=EngineerCommandLedgerDisposition.EXACT,
                binding=binding,
                source_slot=source_slot,
            )

        if source_slot.target_kind == "engineer_work_item_fence":
            if fence_by_key is None or fence_by_source is None:
                raise EngineerWorkItemCoordinatorError("command_ledger_lost")
            if binding_raw is not None or dict(fence_by_key) != dict(fence_by_source):
                raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
            fence = self._validate_fence(item, fence_by_key)
            if item.current_step.state is not EngineerWorkItemStepState.PREPARED:
                raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
            return _LedgerObservation(
                disposition=EngineerCommandLedgerDisposition.FENCED,
                fence=fence,
                source_slot=source_slot,
            )
        raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")

    @staticmethod
    def _snapshot(
        item: EngineerWorkItem,
        observation: _LedgerObservation,
    ) -> EngineerContinuationState:
        binding = observation.binding
        return EngineerContinuationState(
            work_item_id=item.id,
            owner_id=item.owner_id,
            tenant_id=item.tenant_id,
            conversation_id=item.conversation_id,
            channel=item.channel,
            state=item.state,
            transition=item.transition,
            revision=item.revision,
            step_ordinal=item.step_ordinal,
            step_state=item.current_step.state,
            source_binding_sha256=item.current_step.source_binding_sha256,
            idempotency_key=item.current_step.idempotency_key,
            command_digest=item.current_step.command_digest,
            job_receipt_sha256=item.current_step.job_receipt_sha256,
            terminal_receipt_sha256=item.current_step.terminal_receipt_sha256,
            ledger_disposition=observation.disposition,
            command_job_id=binding.job_id if binding is not None else None,
            command_status=binding.status if binding is not None else None,
        )

    def _retire_fenced_prepared(
        self,
        conn: sqlite3.Connection,
        item: EngineerWorkItem,
        fence: Mapping[str, object],
        *,
        now: str | None,
    ) -> EngineerContinuationState | None:
        if item.step_ordinal == 1:
            discard_unsubmitted_engineer_work_item_in_transaction(
                conn,
                owner_id=item.owner_id,
                tenant_id=item.tenant_id,
                conversation_id=item.conversation_id,
                channel=item.channel,
                work_item_id=item.id,
                fence_binding=fence,
            )
            return None
        rolled_back = rollback_fenced_unsubmitted_engineer_step_in_transaction(
            conn,
            owner_id=item.owner_id,
            tenant_id=item.tenant_id,
            conversation_id=item.conversation_id,
            channel=item.channel,
            work_item_id=item.id,
            fence_binding=fence,
            now=now,
        )
        return self._snapshot(rolled_back, self._observe(rolled_back))

    def _reconcile_prepared(
        self,
        conn: sqlite3.Connection,
        item: EngineerWorkItem,
        *,
        source: EngineerCommandSourceSlot | None,
        now: str | None,
    ) -> EngineerAdmissionOutcome:
        observation = self._observe(item, source=source)
        if observation.disposition is EngineerCommandLedgerDisposition.ABSENT:
            return EngineerAdmissionOutcome(
                disposition=observation.disposition,
                continuation=self._snapshot(item, observation),
            )
        if observation.disposition is EngineerCommandLedgerDisposition.FENCED:
            if observation.fence is None:  # pragma: no cover - dataclass construction invariant
                raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
            continuation = self._retire_fenced_prepared(
                conn,
                item,
                observation.fence,
                now=now,
            )
            return EngineerAdmissionOutcome(
                disposition=EngineerCommandLedgerDisposition.FENCED,
                continuation=continuation,
            )
        if observation.binding is None:  # pragma: no cover - dataclass construction invariant
            raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
        admitted = bind_engineer_command_receipts_in_transaction(
            conn,
            owner_id=item.owner_id,
            tenant_id=item.tenant_id,
            conversation_id=item.conversation_id,
            channel=item.channel,
            work_item_id=item.id,
            expected_revision=item.revision,
            ledger_binding=observation.binding.raw,
            now=now,
        )
        admitted_observation = self._observe(admitted)
        return EngineerAdmissionOutcome(
            disposition=EngineerCommandLedgerDisposition.EXACT,
            continuation=self._snapshot(admitted, admitted_observation),
        )

    def reserve_initial_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        reservation: EngineerCommandReservation,
        work_item_id: str | None = None,
        now: str | None = None,
        expires_at: str | None = None,
    ) -> EngineerAdmissionOutcome:
        """Reserve step one, then reconcile before permitting any submission."""

        with self._coordination(conn):
            item = create_engineer_work_item_in_transaction(
                conn,
                owner_id=reservation.source.owner_id,
                tenant_id=reservation.source.tenant_id,
                conversation_id=reservation.source.conversation_id,
                channel=reservation.source.channel,
                source_binding_sha256=reservation.source.binding_sha256(),
                completion_contract_sha256=ENGINEER_WORK_ITEM_COMPLETION_CONTRACT_SHA256,
                idempotency_key=reservation.idempotency_key,
                command_digest=reservation.command_digest,
                work_item_id=work_item_id,
                now=now,
                expires_at=expires_at,
            )
            if item.state is EngineerWorkItemState.ACTIVE:
                return self._reconcile_prepared(
                    conn,
                    item,
                    source=reservation.source,
                    now=now,
                )
            observation = self._observe(item)
            return EngineerAdmissionOutcome(
                disposition=observation.disposition,
                continuation=self._snapshot(item, observation),
            )

    def reserve_next_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        work_item_id: str,
        expected_revision: int,
        reservation: EngineerCommandReservation,
        now: str | None = None,
    ) -> EngineerAdmissionOutcome:
        """Reserve one dependent command after an observed terminal receipt."""

        with self._coordination(conn):
            item = start_next_engineer_step_in_transaction(
                conn,
                owner_id=reservation.source.owner_id,
                tenant_id=reservation.source.tenant_id,
                conversation_id=reservation.source.conversation_id,
                channel=reservation.source.channel,
                work_item_id=work_item_id,
                expected_revision=expected_revision,
                source_binding_sha256=reservation.source.binding_sha256(),
                idempotency_key=reservation.idempotency_key,
                command_digest=reservation.command_digest,
                now=now,
            )
            return self._reconcile_prepared(
                conn,
                item,
                source=reservation.source,
                now=now,
            )

    def reconcile_admission_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        work_item_id: str,
        owner_id: str,
        tenant_id: str,
        conversation_id: str,
        channel: EngineerWorkItemChannel,
        expected_revision: int,
        source: EngineerCommandSourceSlot | None = None,
        now: str | None = None,
    ) -> EngineerAdmissionOutcome:
        """Bind an exact existing external job; a healthy absence permits submit."""

        with self._coordination(conn):
            item = self._required_item(
                conn,
                work_item_id=work_item_id,
                owner_id=owner_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                channel=channel,
                expected_revision=expected_revision,
            )
            if item.current_step.state is EngineerWorkItemStepState.PREPARED:
                return self._reconcile_prepared(conn, item, source=source, now=now)
            observation = self._observe(item)
            return EngineerAdmissionOutcome(
                disposition=observation.disposition,
                continuation=self._snapshot(item, observation),
            )

    def retire_proven_unsubmitted_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        work_item_id: str,
        owner_id: str,
        tenant_id: str,
        conversation_id: str,
        channel: EngineerWorkItemChannel,
        expected_revision: int,
        source: EngineerCommandSourceSlot | None = None,
        now: str | None = None,
    ) -> EngineerAdmissionOutcome:
        """Fence external admission first, then retire only the exact prepared row."""

        with self._coordination(conn):
            item = self._required_item(
                conn,
                work_item_id=work_item_id,
                owner_id=owner_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                channel=channel,
                expected_revision=expected_revision,
            )
            if item.current_step.state is not EngineerWorkItemStepState.PREPARED:
                raise EngineerWorkItemConflictError("only prepared command work can be retired")
            if source is None:
                raise EngineerWorkItemCoordinatorError("command_source_required")
            source = self._validated_source_for_item(item, source)
            observed = self._observe(item, source=source)
            if observed.disposition is not EngineerCommandLedgerDisposition.ABSENT:
                return self._reconcile_prepared(conn, item, source=source, now=now)
            fence_kwargs = {
                "actor_id": item.owner_id,
                "idempotency_key": item.current_step.idempotency_key,
                "work_item_id": item.id,
                "expected_revision": item.revision,
                "step_ordinal": item.step_ordinal,
                "source_binding_sha256": item.current_step.source_binding_sha256,
                "legacy_source_binding_sha256": source.legacy_binding_sha256(),
                "command_digest": item.current_step.command_digest,
            }
            try:
                self._ledger_call(
                    self._ledger.create_engineer_work_item_fence,
                    **fence_kwargs,
                )
            except EngineerWorkItemCoordinatorError:
                # A job may have won the race.  Re-read authority once; never infer
                # absence from the failed fence commit.
                raced = self._observe(item, source=source)
                if raced.disposition is EngineerCommandLedgerDisposition.EXACT:
                    return self._reconcile_prepared(conn, item, source=source, now=now)
                raise
            fenced = self._observe(item, source=source)
            if fenced.disposition is not EngineerCommandLedgerDisposition.FENCED or fenced.fence is None:
                raise EngineerWorkItemCoordinatorError("command_ledger_inconsistent")
            continuation = self._retire_fenced_prepared(
                conn,
                item,
                fenced.fence,
                now=now,
            )
            return EngineerAdmissionOutcome(
                disposition=EngineerCommandLedgerDisposition.FENCED,
                continuation=continuation,
            )

    def mark_unknown_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        work_item_id: str,
        owner_id: str,
        tenant_id: str,
        conversation_id: str,
        channel: EngineerWorkItemChannel,
        expected_revision: int,
        source: EngineerCommandSourceSlot | None = None,
        now: str | None = None,
    ) -> EngineerContinuationState:
        """Persist UNKNOWN only after exact admission; this method never submits."""

        with self._coordination(conn):
            item = self._required_item(
                conn,
                work_item_id=work_item_id,
                owner_id=owner_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                channel=channel,
                expected_revision=expected_revision,
            )
            if item.state is EngineerWorkItemState.UNCERTAIN:
                return self._snapshot(item, self._observe(item))
            if item.current_step.state is EngineerWorkItemStepState.PREPARED:
                outcome = self._reconcile_prepared(conn, item, source=source, now=now)
                if (
                    outcome.disposition is not EngineerCommandLedgerDisposition.EXACT
                    or outcome.continuation is None
                ):
                    raise EngineerWorkItemCoordinatorError("command_admission_unproven")
                reloaded = get_engineer_work_item_in_transaction(
                    conn,
                    work_item_id=item.id,
                    owner_id=item.owner_id,
                    tenant_id=item.tenant_id,
                    conversation_id=item.conversation_id,
                    channel=item.channel,
                )
                if reloaded is None:  # pragma: no cover - same-transaction invariant
                    raise EngineerWorkItemCoordinatorError("main_work_item_unavailable")
                item = reloaded
            observation = self._observe(item)
            if observation.disposition is not EngineerCommandLedgerDisposition.EXACT:
                raise EngineerWorkItemCoordinatorError("command_admission_unproven")
            unknown = mark_engineer_command_unknown_in_transaction(
                conn,
                owner_id=item.owner_id,
                tenant_id=item.tenant_id,
                conversation_id=item.conversation_id,
                channel=item.channel,
                work_item_id=item.id,
                expected_revision=item.revision,
                now=now,
            )
            return self._snapshot(unknown, self._observe(unknown))

    def settle_verified_terminal_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        work_item_id: str,
        owner_id: str,
        tenant_id: str,
        conversation_id: str,
        channel: EngineerWorkItemChannel,
        expected_revision: int,
        verified_job_id: str,
        verified_terminal_receipt_sha256: str,
        source: EngineerCommandSourceSlot | None = None,
        now: str | None = None,
    ) -> EngineerContinuationState:
        """Settle only a kernel-verified terminal digest for the exact job."""

        with self._coordination(conn):
            item = self._required_item(
                conn,
                work_item_id=work_item_id,
                owner_id=owner_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                channel=channel,
                expected_revision=expected_revision,
            )
            if item.current_step.state is EngineerWorkItemStepState.PREPARED:
                outcome = self._reconcile_prepared(conn, item, source=source, now=now)
                if (
                    outcome.disposition is not EngineerCommandLedgerDisposition.EXACT
                    or outcome.continuation is None
                ):
                    raise EngineerWorkItemCoordinatorError("command_admission_unproven")
                reloaded = get_engineer_work_item_in_transaction(
                    conn,
                    work_item_id=item.id,
                    owner_id=item.owner_id,
                    tenant_id=item.tenant_id,
                    conversation_id=item.conversation_id,
                    channel=item.channel,
                )
                if reloaded is None:  # pragma: no cover - same-transaction invariant
                    raise EngineerWorkItemCoordinatorError("main_work_item_unavailable")
                item = reloaded
            observation = self._observe(item)
            binding = observation.binding
            if (
                observation.disposition is not EngineerCommandLedgerDisposition.EXACT
                or binding is None
                or verified_job_id != binding.job_id
                or binding.status not in _PUBLISHABLE_TERMINAL
            ):
                raise EngineerWorkItemCoordinatorError("verified_terminal_mismatch")
            if item.current_step.state is EngineerWorkItemStepState.SETTLED:
                if item.current_step.terminal_receipt_sha256 != verified_terminal_receipt_sha256:
                    raise EngineerWorkItemCoordinatorError("verified_terminal_mismatch")
                return self._snapshot(item, observation)
            settled = settle_engineer_terminal_receipt_in_transaction(
                conn,
                owner_id=item.owner_id,
                tenant_id=item.tenant_id,
                conversation_id=item.conversation_id,
                channel=item.channel,
                work_item_id=item.id,
                expected_revision=item.revision,
                verified_terminal_receipt_sha256=verified_terminal_receipt_sha256,
                now=now,
            )
            return self._snapshot(settled, self._observe(settled))

    def current_structural_state_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        owner_id: str,
        tenant_id: str,
        conversation_id: str,
        channel: EngineerWorkItemChannel,
        current_source: EngineerCommandSourceSlot | None = None,
    ) -> EngineerContinuationState | None:
        """Read the current body-free continuation plus exact ledger status."""

        self._require_transaction(conn)
        item = get_current_engineer_work_item_in_transaction(
            conn,
            owner_id=owner_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            channel=channel,
        )
        return None if item is None else self._snapshot(item, self._observe(item, source=current_source))

    def prepare_completion_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        work_item_id: str,
        owner_id: str,
        tenant_id: str,
        conversation_id: str,
        channel: EngineerWorkItemChannel,
        expected_revision: int,
        now: str | None = None,
    ) -> EngineerContinuationState:
        """Enter READY_TO_ANSWER inside the caller-owned main transaction."""

        with self._coordination(conn):
            ready = mark_engineer_work_item_ready_to_answer_in_transaction(
                conn,
                work_item_id=work_item_id,
                owner_id=owner_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                channel=channel,
                expected_revision=expected_revision,
                now=now,
            )
            return self._snapshot(ready, self._observe(ready))

    def close_completion_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        work_item_id: str,
        owner_id: str,
        tenant_id: str,
        conversation_id: str,
        channel: EngineerWorkItemChannel,
        expected_revision: int,
        now: str | None = None,
    ) -> EngineerWorkItem:
        """Close after the caller staged assistant/outbound rows in this txn."""

        with self._coordination(conn):
            return close_engineer_work_item_in_transaction(
                conn,
                work_item_id=work_item_id,
                owner_id=owner_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                channel=channel,
                expected_revision=expected_revision,
                terminal_state=EngineerWorkItemState.COMPLETED,
                now=now,
            )


__all__ = [
    "EngineerAdmissionOutcome",
    "EngineerCommandLedger",
    "EngineerCommandLedgerObservation",
    "EngineerCommandLedgerDisposition",
    "EngineerCommandReservation",
    "EngineerCommandSourceSlot",
    "EngineerContinuationState",
    "EngineerWorkItemCoordinatorError",
    "EngineerWorkItemRuntimeCoordinator",
]
