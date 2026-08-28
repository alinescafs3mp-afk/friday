"""Dormant, body-free durable contract for restart-safe Engineer continuation.

This is not a model loop and it does not execute or resume commands.  The store
retains only code-owned identity, lifecycle state, and opaque receipt digests so
a later activation package can reconcile the command kernel before replanning.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import unicodedata
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from friday.engineer_source_binding import (
    ENGINEER_SOURCE_BINDING_SCHEMA,
    canonical_engineer_source_binding_sha256,
    canonical_engineer_source_step_id,
)
from friday.interaction_control_plane.engineer_work_item_schema import (
    ENGINEER_WORK_ITEM_COMPLETION_CONTRACT_SHA256,
    ENGINEER_WORK_ITEM_MAX_REVISION,
    ENGINEER_WORK_ITEM_MAX_STEPS,
    ENGINEER_WORK_ITEM_MAX_TTL_SECONDS,
    register_engineer_work_item_connection_functions,
)
from friday.interaction_control_plane.work_item_contract import canonical_work_item_instant
from friday.user_ids import validate_user_id

ENGINEER_WORK_ITEM_CONTRACT_SCHEMA = "friday.engineer-work-item.v1"
ENGINEER_WORK_ITEM_STEP_SCHEMA = "friday.engineer-work-item-step.v1"
ENGINEER_JOB_BINDING_SCHEMA = "friday.engineer-job-binding.v1"
ENGINEER_WORK_ITEM_DEFAULT_TTL_HOURS = 12
ENGINEER_WORK_ITEM_RETENTION_DAYS = 30
ENGINEER_WORK_ITEM_RETENTION_BATCH_MAX = 100

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_ITEM_ID_RE = re.compile(r"ewi_[0-9a-f]{32}")
_IDEMPOTENCY_KEY_RE = re.compile(r"ecmd-[0-9a-f]{64}")
_JOB_ID_RE = re.compile(r"[0-9a-f]{32}")
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}")
_DELIVERY_CHAT_ID_RE = re.compile(r"[1-9][0-9]{0,19}")
_COMMAND_LEDGER_BINDING_KEYS = frozenset(
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
_COMMAND_FENCE_BINDING_KEYS = frozenset(
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


class EngineerWorkItemContractError(ValueError):
    """A value is outside the closed EngineerWorkItem v1 contract."""


class EngineerWorkItemConflictError(RuntimeError):
    """The expected revision/state/idempotency identity is no longer current."""


class EngineerWorkItemAnchorError(ValueError):
    """The exact owner, tenant, conversation, or source binding is invalid."""


class EngineerWorkItemChannel(StrEnum):
    TELEGRAM = "telegram"


class EngineerWorkItemState(StrEnum):
    ACTIVE = "active"
    WAITING_FOR_CAPABILITY = "waiting_for_capability"
    UNCERTAIN = "uncertain"
    WAITING_FOR_INPUT = "waiting_for_input"
    READY_TO_ANSWER = "ready_to_answer"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @property
    def is_open(self) -> bool:
        return self in {
            EngineerWorkItemState.ACTIVE,
            EngineerWorkItemState.WAITING_FOR_CAPABILITY,
            EngineerWorkItemState.UNCERTAIN,
            EngineerWorkItemState.WAITING_FOR_INPUT,
            EngineerWorkItemState.READY_TO_ANSWER,
        }


class EngineerWorkItemTransition(StrEnum):
    CREATED = "created"
    COMMAND_ADMITTED = "command_admitted"
    COMMAND_UNKNOWN = "command_unknown"
    TERMINAL_OBSERVED = "terminal_observed"
    NEXT_STEP_STARTED = "next_step_started"
    PREPARED_STEP_DISCARDED = "prepared_step_discarded"
    ANSWER_READY = "answer_ready"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class EngineerWorkItemStepState(StrEnum):
    PREPARED = "prepared"
    ADMITTED = "admitted"
    UNKNOWN = "unknown"
    SETTLED = "settled"


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _identity(value: object, *, label: str) -> str:
    if not isinstance(value, str) or value != value.strip() or _contains_control(value):
        raise EngineerWorkItemAnchorError(f"{label} is not a canonical identity")
    try:
        identity = validate_user_id(value)
    except ValueError as exc:
        raise EngineerWorkItemAnchorError(f"{label} is not a canonical identity") from exc
    if len(identity) > 128:
        raise EngineerWorkItemAnchorError(f"{label} exceeds the command-authority limit")
    return identity


def _conversation_id(value: object) -> str:
    if not isinstance(value, str) or _CONVERSATION_ID_RE.fullmatch(value) is None:
        raise EngineerWorkItemAnchorError("conversation_id is not canonical")
    return value


def _digest(value: object, *, label: str, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise EngineerWorkItemContractError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _instant(value: object, *, label: str) -> str:
    try:
        canonical = canonical_work_item_instant(value, label=label)
    except ValueError as exc:
        raise EngineerWorkItemContractError(str(exc)) from exc
    if canonical != value:
        raise EngineerWorkItemContractError(f"{label} must already be canonical")
    return canonical


def _now(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).isoformat(timespec="seconds")
    return _instant(value, label="now")


def _item_id(value: object) -> str:
    if not isinstance(value, str) or _ITEM_ID_RE.fullmatch(value) is None:
        raise EngineerWorkItemContractError("work_item_id is not canonical")
    return value


def _idempotency_key(value: object) -> str:
    if not isinstance(value, str) or _IDEMPOTENCY_KEY_RE.fullmatch(value) is None:
        raise EngineerWorkItemContractError("idempotency_key is not an opaque v1 key")
    return value


def _job_id(value: object) -> str:
    if not isinstance(value, str) or _JOB_ID_RE.fullmatch(value) is None:
        raise EngineerWorkItemContractError("job_id is not an opaque command-kernel identity")
    return value


def _expected_revision(value: object) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value < ENGINEER_WORK_ITEM_MAX_REVISION
    ):
        raise EngineerWorkItemContractError("expected_revision is outside the closed limit")
    return value


def _delivery_chat_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or _DELIVERY_CHAT_ID_RE.fullmatch(value) is None
        or int(value) > 9_999_999_999_999_999_999
    ):
        raise EngineerWorkItemAnchorError("delivery_chat_id is not an admitted Telegram scope")
    return value


def _stored_str(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise EngineerWorkItemContractError(f"stored {label} is not text")
    return value


def _stored_int(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise EngineerWorkItemContractError(f"stored {label} is not an integer")
    return value


def new_engineer_work_item_id() -> str:
    return f"ewi_{secrets.token_hex(16)}"


def engineer_source_binding_sha256(
    *,
    owner_id: str,
    tenant_id: str,
    conversation_id: str,
    channel: EngineerWorkItemChannel,
    source_row_id: str,
    source_step_id: str,
    source_hash: str,
    telegram_update_id: str,
    delivery_chat_id: str,
) -> str:
    """Hash the exact authenticated ingress and Telegram delivery scope."""

    owner = _identity(owner_id, label="owner_id")
    tenant = _identity(tenant_id, label="tenant_id")
    conversation = _conversation_id(conversation_id)
    if not isinstance(channel, EngineerWorkItemChannel):
        raise EngineerWorkItemAnchorError("channel is not admitted")
    for label, value in (
        ("source_row_id", source_row_id),
        ("telegram_update_id", telegram_update_id),
    ):
        if not isinstance(value, str) or not value or _contains_control(value):
            raise EngineerWorkItemAnchorError(f"{label} is not a bounded source identity")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:  # pragma: no cover - control guard normally catches surrogates
            raise EngineerWorkItemAnchorError(f"{label} is not a bounded source identity") from exc
        if len(value) > 128:
            raise EngineerWorkItemAnchorError(f"{label} is not a bounded source identity")
    try:
        source_step = canonical_engineer_source_step_id(source_step_id)
    except ValueError as exc:
        raise EngineerWorkItemAnchorError("source_step_id is not a code-owned Engineer slot") from exc
    source_digest = _digest(source_hash, label="source_hash")
    delivery = _delivery_chat_id(delivery_chat_id)
    return canonical_engineer_source_binding_sha256(
        owner_id=owner,
        tenant_id=tenant,
        conversation_id=conversation,
        channel=channel.value,
        source_row_id=source_row_id,
        source_step_id=source_step,
        source_hash=source_digest,
        telegram_update_id=telegram_update_id,
        delivery_chat_id=delivery,
    )


@dataclass(frozen=True, slots=True)
class _ObservedCommandLedgerBinding:
    job_id: str
    actor_id: str
    tenant_id: str
    conversation_id: str
    channel: EngineerWorkItemChannel
    source_step_id: str
    source_binding_sha256: str
    idempotency_key: str
    command_digest: str
    delivery_chat_id: str


@dataclass(frozen=True, slots=True)
class _ObservedCommandFenceBinding:
    actor_id: str
    work_item_id: str
    expected_revision: int
    step_ordinal: int
    source_binding_sha256: str
    idempotency_key: str
    command_digest: str


def _observed_command_fence_binding(
    value: Mapping[str, object],
) -> _ObservedCommandFenceBinding:
    if not isinstance(value, Mapping) or frozenset(value) != _COMMAND_FENCE_BINDING_KEYS:
        raise EngineerWorkItemContractError("command fence binding projection is not exact")
    ordinal = value["step_ordinal"]
    if (
        not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
        or not 1 <= ordinal <= ENGINEER_WORK_ITEM_MAX_STEPS
    ):
        raise EngineerWorkItemContractError("fenced step ordinal is outside the closed limit")
    return _ObservedCommandFenceBinding(
        actor_id=_identity(value["actor_id"], label="fence_actor_id"),
        work_item_id=_item_id(value["work_item_id"]),
        expected_revision=_expected_revision(value["expected_revision"]),
        step_ordinal=ordinal,
        source_binding_sha256=_digest(
            value["source_binding_sha256"],
            label="fence source_binding_sha256",
        ),
        idempotency_key=_idempotency_key(value["idempotency_key"]),
        command_digest=_digest(value["command_digest"], label="fence command_digest"),
    )


def _lookup_retired_command_fence(
    conn: sqlite3.Connection,
    *,
    owner_id: str,
    idempotency_key: str,
) -> _ObservedCommandFenceBinding | None:
    row = conn.execute(
        """SELECT owner_id AS actor_id,work_item_id,expected_revision,step_ordinal,
                  source_binding_sha256,idempotency_key,command_digest
             FROM engineer_work_item_command_fences
            WHERE owner_id=? AND idempotency_key=?""",
        (owner_id, idempotency_key),
    ).fetchone()
    return _observed_command_fence_binding(dict(row)) if row is not None else None


def _retired_command_identity_exists(
    conn: sqlite3.Connection,
    *,
    owner_id: str,
    idempotency_key: str,
    source_binding_sha256: str,
) -> bool:
    return (
        conn.execute(
            """SELECT 1 FROM engineer_work_item_command_fences
                WHERE owner_id=?
                  AND (idempotency_key=? OR source_binding_sha256=?)""",
            (owner_id, idempotency_key, source_binding_sha256),
        ).fetchone()
        is not None
    )


def _persist_retired_command_fence(
    conn: sqlite3.Connection,
    *,
    fence: _ObservedCommandFenceBinding,
    retired_at: str,
) -> None:
    existing = _lookup_retired_command_fence(
        conn,
        owner_id=fence.actor_id,
        idempotency_key=fence.idempotency_key,
    )
    if existing is not None:
        if existing != fence:
            raise EngineerWorkItemConflictError("command fence identity is already retired")
        return
    try:
        conn.execute(
            """INSERT INTO engineer_work_item_command_fences(
                   owner_id,idempotency_key,work_item_id,expected_revision,step_ordinal,
                   source_binding_sha256,command_digest,retired_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                fence.actor_id,
                fence.idempotency_key,
                fence.work_item_id,
                fence.expected_revision,
                fence.step_ordinal,
                fence.source_binding_sha256,
                fence.command_digest,
                _instant(retired_at, label="retired_at"),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise EngineerWorkItemConflictError("command fence could not be retired exactly") from exc
    stored = _lookup_retired_command_fence(
        conn,
        owner_id=fence.actor_id,
        idempotency_key=fence.idempotency_key,
    )
    if stored != fence:  # pragma: no cover - insert/read-back invariant
        raise EngineerWorkItemConflictError("retired command fence read-back changed identity")


def _observed_command_ledger_binding(
    value: Mapping[str, object],
) -> _ObservedCommandLedgerBinding:
    if not isinstance(value, Mapping) or frozenset(value) != _COMMAND_LEDGER_BINDING_KEYS:
        raise EngineerWorkItemContractError("command ledger binding projection is not exact")
    actor = _identity(value["actor_id"], label="ledger_actor_id")
    tenant = _identity(value["tenant_id"], label="ledger_tenant_id")
    conversation = _conversation_id(value["conversation_id"])
    try:
        channel = EngineerWorkItemChannel(_stored_str(value["channel"], label="ledger channel"))
    except (TypeError, ValueError) as exc:
        raise EngineerWorkItemAnchorError("ledger channel is not admitted") from exc
    delivery = _delivery_chat_id(value["delivery_chat_id"])
    source = engineer_source_binding_sha256(
        owner_id=actor,
        tenant_id=tenant,
        conversation_id=conversation,
        channel=channel,
        source_row_id=_stored_str(value["source_row_id"], label="ledger source_row_id"),
        source_step_id=_stored_str(value["source_step_id"], label="ledger source_step_id"),
        source_hash=_stored_str(value["source_hash"], label="ledger source_hash"),
        telegram_update_id=_stored_str(
            value["telegram_update_id"],
            label="ledger telegram_update_id",
        ),
        delivery_chat_id=delivery,
    )
    return _ObservedCommandLedgerBinding(
        job_id=_job_id(value["job_id"]),
        actor_id=actor,
        tenant_id=tenant,
        conversation_id=conversation,
        channel=channel,
        source_step_id=_stored_str(value["source_step_id"], label="ledger source_step_id"),
        source_binding_sha256=source,
        idempotency_key=_idempotency_key(value["idempotency_key"]),
        command_digest=_digest(value["command_digest"], label="ledger command_digest"),
        delivery_chat_id=delivery,
    )


def engineer_job_receipt_sha256(
    *,
    owner_id: str,
    tenant_id: str,
    conversation_id: str,
    channel: EngineerWorkItemChannel,
    source_binding_sha256: str,
    delivery_chat_id: str,
    idempotency_key: str,
    command_digest: str,
    job_id: str,
) -> str:
    """Digest an exact body-free command-ledger lookup result."""

    owner = _identity(owner_id, label="owner_id")
    tenant = _identity(tenant_id, label="tenant_id")
    conversation = _conversation_id(conversation_id)
    if not isinstance(channel, EngineerWorkItemChannel):
        raise EngineerWorkItemAnchorError("channel is not admitted")
    source = _digest(source_binding_sha256, label="source_binding_sha256")
    delivery = _delivery_chat_id(delivery_chat_id)
    key = _idempotency_key(idempotency_key)
    command = _digest(command_digest, label="command_digest")
    job = _job_id(job_id)
    payload = {
        "channel": channel.value,
        "command_digest": command,
        "conversation_id": conversation,
        "delivery_chat_id": delivery,
        "idempotency_key": key,
        "job_id": job,
        "owner_id": owner,
        "schema": ENGINEER_JOB_BINDING_SCHEMA,
        "source_binding_sha256": source,
        "tenant_id": tenant,
    }
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class EngineerWorkItemStep:
    work_item_id: str
    owner_id: str
    ordinal: int
    source_binding_sha256: str
    state: EngineerWorkItemStepState
    idempotency_key: str
    command_digest: str
    job_receipt_sha256: str
    terminal_receipt_sha256: str
    created_at: str
    updated_at: str
    admitted_at: str | None
    settled_at: str | None

    def __post_init__(self) -> None:
        _item_id(self.work_item_id)
        _identity(self.owner_id, label="step.owner_id")
        if (
            not isinstance(self.ordinal, int)
            or isinstance(self.ordinal, bool)
            or not 1 <= self.ordinal <= ENGINEER_WORK_ITEM_MAX_STEPS
        ):
            raise EngineerWorkItemContractError("step ordinal is outside the closed limit")
        _digest(self.source_binding_sha256, label="step.source_binding_sha256")
        if not isinstance(self.state, EngineerWorkItemStepState):
            raise EngineerWorkItemContractError("step state is not a closed enum")
        _idempotency_key(self.idempotency_key)
        command = _digest(self.command_digest, label="command_digest")
        job = _digest(self.job_receipt_sha256, label="job_receipt_sha256", allow_empty=True)
        terminal = _digest(
            self.terminal_receipt_sha256,
            label="terminal_receipt_sha256",
            allow_empty=True,
        )
        created = _instant(self.created_at, label="step.created_at")
        updated = _instant(self.updated_at, label="step.updated_at")
        if updated < created:
            raise EngineerWorkItemContractError("step updated_at precedes created_at")
        admitted = (
            _instant(self.admitted_at, label="step.admitted_at") if self.admitted_at is not None else None
        )
        settled = _instant(self.settled_at, label="step.settled_at") if self.settled_at is not None else None
        if admitted is not None and not created <= admitted <= updated:
            raise EngineerWorkItemContractError("step admitted_at is outside its lifecycle")
        if settled is not None and not created <= settled <= updated:
            raise EngineerWorkItemContractError("step settled_at is outside its lifecycle")
        if self.state is EngineerWorkItemStepState.PREPARED:
            valid = bool(command and not job and not terminal and admitted is None and settled is None)
        elif self.state in {EngineerWorkItemStepState.ADMITTED, EngineerWorkItemStepState.UNKNOWN}:
            valid = bool(command and job and not terminal and admitted is not None and settled is None)
        else:
            valid = bool(command and job and terminal and admitted is not None and settled == updated)
        if not valid:
            raise EngineerWorkItemContractError("step receipts do not match its state")

    @classmethod
    def from_storage_row(cls, value: Mapping[str, object]) -> EngineerWorkItemStep:
        expected = {
            "work_item_id",
            "owner_id",
            "ordinal",
            "source_binding_sha256",
            "state",
            "idempotency_key",
            "command_digest",
            "job_receipt_sha256",
            "terminal_receipt_sha256",
            "created_at",
            "updated_at",
            "admitted_at",
            "settled_at",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise EngineerWorkItemContractError("step storage row is not the closed projection")
        try:
            state = EngineerWorkItemStepState(_stored_str(value["state"], label="step.state"))
        except (TypeError, ValueError) as exc:
            raise EngineerWorkItemContractError("step state is not a closed enum") from exc
        return cls(
            work_item_id=_stored_str(value["work_item_id"], label="step.work_item_id"),
            owner_id=_stored_str(value["owner_id"], label="step.owner_id"),
            ordinal=_stored_int(value["ordinal"], label="step.ordinal"),
            source_binding_sha256=_stored_str(
                value["source_binding_sha256"],
                label="step.source_binding_sha256",
            ),
            state=state,
            idempotency_key=_stored_str(value["idempotency_key"], label="step.idempotency_key"),
            command_digest=_stored_str(value["command_digest"], label="step.command_digest"),
            job_receipt_sha256=_stored_str(value["job_receipt_sha256"], label="step.job_receipt_sha256"),
            terminal_receipt_sha256=_stored_str(
                value["terminal_receipt_sha256"], label="step.terminal_receipt_sha256"
            ),
            created_at=_stored_str(value["created_at"], label="step.created_at"),
            updated_at=_stored_str(value["updated_at"], label="step.updated_at"),
            admitted_at=(
                _stored_str(value["admitted_at"], label="step.admitted_at")
                if value["admitted_at"] is not None
                else None
            ),
            settled_at=(
                _stored_str(value["settled_at"], label="step.settled_at")
                if value["settled_at"] is not None
                else None
            ),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": ENGINEER_WORK_ITEM_STEP_SCHEMA,
            "work_item_id": self.work_item_id,
            "owner_id": self.owner_id,
            "ordinal": self.ordinal,
            "source_binding_sha256": self.source_binding_sha256,
            "state": self.state.value,
            "idempotency_key": self.idempotency_key,
            "command_digest": self.command_digest,
            "job_receipt_sha256": self.job_receipt_sha256,
            "terminal_receipt_sha256": self.terminal_receipt_sha256,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "admitted_at": self.admitted_at,
            "settled_at": self.settled_at,
        }


@dataclass(frozen=True, slots=True)
class EngineerWorkItem:
    id: str
    owner_id: str
    tenant_id: str
    conversation_id: str
    channel: EngineerWorkItemChannel
    source_binding_sha256: str
    state: EngineerWorkItemState
    revision: int
    step_ordinal: int
    transition: EngineerWorkItemTransition
    completion_contract_sha256: str
    created_at: str
    updated_at: str
    expires_at: str
    completed_at: str | None
    closed_at: str | None
    steps: tuple[EngineerWorkItemStep, ...]

    def __post_init__(self) -> None:
        _item_id(self.id)
        _identity(self.owner_id, label="owner_id")
        _identity(self.tenant_id, label="tenant_id")
        _conversation_id(self.conversation_id)
        if not isinstance(self.channel, EngineerWorkItemChannel):
            raise EngineerWorkItemContractError("channel is not a closed enum")
        _digest(self.source_binding_sha256, label="source_binding_sha256")
        _digest(self.completion_contract_sha256, label="completion_contract_sha256")
        if self.completion_contract_sha256 != ENGINEER_WORK_ITEM_COMPLETION_CONTRACT_SHA256:
            raise EngineerWorkItemContractError("completion contract is not EngineerWorkItem v1")
        if not isinstance(self.state, EngineerWorkItemState):
            raise EngineerWorkItemContractError("state is not a closed enum")
        if not isinstance(self.transition, EngineerWorkItemTransition):
            raise EngineerWorkItemContractError("transition is not a closed enum")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or not 1 <= self.revision <= ENGINEER_WORK_ITEM_MAX_REVISION
        ):
            raise EngineerWorkItemContractError("revision is outside the closed limit")
        if (
            not isinstance(self.step_ordinal, int)
            or isinstance(self.step_ordinal, bool)
            or not 1 <= self.step_ordinal <= ENGINEER_WORK_ITEM_MAX_STEPS
        ):
            raise EngineerWorkItemContractError("step ordinal is outside the closed limit")
        created = _instant(self.created_at, label="created_at")
        updated = _instant(self.updated_at, label="updated_at")
        expires = _instant(self.expires_at, label="expires_at")
        created_dt = datetime.fromisoformat(created)
        expires_dt = datetime.fromisoformat(expires)
        if (
            updated < created
            or expires_dt <= created_dt
            or expires_dt > created_dt + timedelta(seconds=ENGINEER_WORK_ITEM_MAX_TTL_SECONDS)
        ):
            raise EngineerWorkItemContractError("work item lifecycle bounds are invalid")
        completed = (
            _instant(self.completed_at, label="completed_at") if self.completed_at is not None else None
        )
        closed = _instant(self.closed_at, label="closed_at") if self.closed_at is not None else None
        if self.state.is_open:
            if completed is not None or closed is not None:
                raise EngineerWorkItemContractError("open work item carries terminal timestamps")
        elif self.state is EngineerWorkItemState.COMPLETED:
            if completed != updated or closed != updated:
                raise EngineerWorkItemContractError("completed work item timestamps are invalid")
        elif completed is not None or closed != updated:
            raise EngineerWorkItemContractError("terminal work item timestamps are invalid")
        expected_transition = {
            EngineerWorkItemState.ACTIVE: {
                EngineerWorkItemTransition.CREATED,
                EngineerWorkItemTransition.NEXT_STEP_STARTED,
            },
            EngineerWorkItemState.WAITING_FOR_CAPABILITY: {EngineerWorkItemTransition.COMMAND_ADMITTED},
            EngineerWorkItemState.UNCERTAIN: {EngineerWorkItemTransition.COMMAND_UNKNOWN},
            EngineerWorkItemState.WAITING_FOR_INPUT: {
                EngineerWorkItemTransition.TERMINAL_OBSERVED,
                EngineerWorkItemTransition.PREPARED_STEP_DISCARDED,
            },
            EngineerWorkItemState.READY_TO_ANSWER: {EngineerWorkItemTransition.ANSWER_READY},
            EngineerWorkItemState.COMPLETED: {EngineerWorkItemTransition.COMPLETED},
            EngineerWorkItemState.FAILED: {EngineerWorkItemTransition.FAILED},
            EngineerWorkItemState.CANCELLED: {EngineerWorkItemTransition.CANCELLED},
            EngineerWorkItemState.EXPIRED: {EngineerWorkItemTransition.EXPIRED},
        }[self.state]
        if self.transition not in expected_transition:
            raise EngineerWorkItemContractError("transition does not match work item state")
        if len(self.steps) != self.step_ordinal or tuple(step.ordinal for step in self.steps) != tuple(
            range(1, self.step_ordinal + 1)
        ):
            raise EngineerWorkItemContractError("step ledger is not contiguous")
        if any(step.work_item_id != self.id for step in self.steps):
            raise EngineerWorkItemContractError("step belongs to another work item")
        if any(step.owner_id != self.owner_id for step in self.steps):
            raise EngineerWorkItemContractError("step belongs to another owner")
        if self.steps[0].source_binding_sha256 != self.source_binding_sha256:
            raise EngineerWorkItemContractError("initial step does not match the work source binding")
        current_state = self.steps[-1].state
        admitted_current_states = {
            EngineerWorkItemState.ACTIVE: {EngineerWorkItemStepState.PREPARED},
            EngineerWorkItemState.WAITING_FOR_CAPABILITY: {EngineerWorkItemStepState.ADMITTED},
            EngineerWorkItemState.UNCERTAIN: {EngineerWorkItemStepState.UNKNOWN},
            EngineerWorkItemState.WAITING_FOR_INPUT: {EngineerWorkItemStepState.SETTLED},
            EngineerWorkItemState.READY_TO_ANSWER: {EngineerWorkItemStepState.SETTLED},
            EngineerWorkItemState.COMPLETED: {EngineerWorkItemStepState.SETTLED},
            EngineerWorkItemState.FAILED: {EngineerWorkItemStepState.SETTLED},
            EngineerWorkItemState.CANCELLED: {EngineerWorkItemStepState.SETTLED},
            EngineerWorkItemState.EXPIRED: {EngineerWorkItemStepState.SETTLED},
        }[self.state]
        if current_state not in admitted_current_states:
            raise EngineerWorkItemContractError("current step does not match work item state")

    @property
    def current_step(self) -> EngineerWorkItemStep:
        return self.steps[-1]

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": ENGINEER_WORK_ITEM_CONTRACT_SCHEMA,
            "id": self.id,
            "owner_id": self.owner_id,
            "tenant_id": self.tenant_id,
            "conversation_id": self.conversation_id,
            "channel": self.channel.value,
            "source_binding_sha256": self.source_binding_sha256,
            "state": self.state.value,
            "revision": self.revision,
            "step_ordinal": self.step_ordinal,
            "transition": self.transition.value,
            "completion_contract_sha256": self.completion_contract_sha256,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "completed_at": self.completed_at,
            "closed_at": self.closed_at,
            "steps": [step.to_payload() for step in self.steps],
        }


def _item_from_rows(
    row: Mapping[str, object],
    step_rows: tuple[Mapping[str, object], ...],
) -> EngineerWorkItem:
    expected = {
        "id",
        "owner_id",
        "tenant_id",
        "conversation_id",
        "channel",
        "source_binding_sha256",
        "state",
        "revision",
        "step_ordinal",
        "transition",
        "completion_contract_sha256",
        "created_at",
        "updated_at",
        "expires_at",
        "completed_at",
        "closed_at",
    }
    if set(row) != expected:
        raise EngineerWorkItemContractError("work item storage row is not the closed projection")
    try:
        channel = EngineerWorkItemChannel(_stored_str(row["channel"], label="channel"))
        state = EngineerWorkItemState(_stored_str(row["state"], label="state"))
        transition = EngineerWorkItemTransition(_stored_str(row["transition"], label="transition"))
    except (TypeError, ValueError) as exc:
        raise EngineerWorkItemContractError("stored work item enum is invalid") from exc
    return EngineerWorkItem(
        id=_stored_str(row["id"], label="id"),
        owner_id=_stored_str(row["owner_id"], label="owner_id"),
        tenant_id=_stored_str(row["tenant_id"], label="tenant_id"),
        conversation_id=_stored_str(row["conversation_id"], label="conversation_id"),
        channel=channel,
        source_binding_sha256=_stored_str(row["source_binding_sha256"], label="source_binding_sha256"),
        state=state,
        revision=_stored_int(row["revision"], label="revision"),
        step_ordinal=_stored_int(row["step_ordinal"], label="step_ordinal"),
        transition=transition,
        completion_contract_sha256=_stored_str(
            row["completion_contract_sha256"], label="completion_contract_sha256"
        ),
        created_at=_stored_str(row["created_at"], label="created_at"),
        updated_at=_stored_str(row["updated_at"], label="updated_at"),
        expires_at=_stored_str(row["expires_at"], label="expires_at"),
        completed_at=(
            _stored_str(row["completed_at"], label="completed_at")
            if row["completed_at"] is not None
            else None
        ),
        closed_at=(
            _stored_str(row["closed_at"], label="closed_at") if row["closed_at"] is not None else None
        ),
        steps=tuple(EngineerWorkItemStep.from_storage_row(step) for step in step_rows),
    )


def _fetch_item(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    owner_id: str,
    tenant_id: str,
    conversation_id: str,
    channel: EngineerWorkItemChannel,
) -> EngineerWorkItem | None:
    row = conn.execute(
        """SELECT * FROM engineer_work_items
             WHERE id=? AND owner_id=? AND tenant_id=? AND conversation_id=? AND channel=?""",
        (work_item_id, owner_id, tenant_id, conversation_id, channel.value),
    ).fetchone()
    if row is None:
        return None
    steps = conn.execute(
        """SELECT * FROM engineer_work_item_steps
             WHERE work_item_id=? ORDER BY ordinal""",
        (work_item_id,),
    ).fetchall()
    return _item_from_rows(dict(row), tuple(dict(step) for step in steps))


def _scope(
    *,
    work_item_id: str,
    owner_id: str,
    tenant_id: str,
    conversation_id: str,
    channel: EngineerWorkItemChannel,
) -> tuple[str, str, str, str, EngineerWorkItemChannel]:
    identifier = _item_id(work_item_id)
    owner = _identity(owner_id, label="owner_id")
    tenant = _identity(tenant_id, label="tenant_id")
    conversation = _conversation_id(conversation_id)
    if not isinstance(channel, EngineerWorkItemChannel):
        raise EngineerWorkItemAnchorError("channel is not admitted")
    return identifier, owner, tenant, conversation, channel


def _scope_is_live(conn: sqlite3.Connection, item: EngineerWorkItem) -> bool:
    return (
        conn.execute(
            """SELECT 1
                 FROM conversations AS conversation
                 JOIN users AS owner ON owner.id=conversation.user_id
                 JOIN users AS tenant ON tenant.id=?
                WHERE conversation.id=? AND conversation.user_id=?
                  AND conversation.is_archived=0
                  AND owner.status='active' AND tenant.status='active'""",
            (item.tenant_id, item.conversation_id, item.owner_id),
        ).fetchone()
        is not None
    )


@contextmanager
def _mutation(conn: sqlite3.Connection) -> Iterator[None]:
    if not conn.in_transaction:
        raise RuntimeError("Engineer Work Item mutation requires an existing transaction")
    savepoint = f"engineer_work_item_{uuid.uuid4().hex}"
    conn.execute(f'SAVEPOINT "{savepoint}"')  # nosec B608 - generated hexadecimal identifier
    try:
        yield
    except BaseException:
        conn.execute(f'ROLLBACK TO SAVEPOINT "{savepoint}"')  # nosec B608
        conn.execute(f'RELEASE SAVEPOINT "{savepoint}"')  # nosec B608
        raise
    conn.execute(f'RELEASE SAVEPOINT "{savepoint}"')  # nosec B608


def get_engineer_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    owner_id: str,
    tenant_id: str,
    conversation_id: str,
    channel: EngineerWorkItemChannel,
) -> EngineerWorkItem | None:
    identifier, owner, tenant, conversation, admitted_channel = _scope(
        work_item_id=work_item_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
    )
    return _fetch_item(
        conn,
        work_item_id=identifier,
        owner_id=owner,
        tenant_id=tenant,
        conversation_id=conversation,
        channel=admitted_channel,
    )


def get_current_engineer_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    owner_id: str,
    tenant_id: str,
    conversation_id: str,
    channel: EngineerWorkItemChannel,
) -> EngineerWorkItem | None:
    owner = _identity(owner_id, label="owner_id")
    tenant = _identity(tenant_id, label="tenant_id")
    conversation = _conversation_id(conversation_id)
    if not isinstance(channel, EngineerWorkItemChannel):
        raise EngineerWorkItemAnchorError("channel is not admitted")
    row = conn.execute(
        """SELECT id FROM engineer_work_items
             WHERE owner_id=? AND tenant_id=? AND conversation_id=? AND channel=?
               AND state IN (
                   'active','waiting_for_capability','uncertain','waiting_for_input','ready_to_answer'
               )""",
        (owner, tenant, conversation, channel.value),
    ).fetchone()
    if row is None:
        return None
    return _fetch_item(
        conn,
        work_item_id=str(row["id"]),
        owner_id=owner,
        tenant_id=tenant,
        conversation_id=conversation,
        channel=channel,
    )


def create_engineer_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    owner_id: str,
    tenant_id: str,
    conversation_id: str,
    channel: EngineerWorkItemChannel,
    source_binding_sha256: str,
    completion_contract_sha256: str,
    idempotency_key: str,
    command_digest: str,
    work_item_id: str | None = None,
    now: str | None = None,
    expires_at: str | None = None,
) -> EngineerWorkItem:
    owner = _identity(owner_id, label="owner_id")
    tenant = _identity(tenant_id, label="tenant_id")
    conversation = _conversation_id(conversation_id)
    if not isinstance(channel, EngineerWorkItemChannel):
        raise EngineerWorkItemAnchorError("channel is not admitted")
    source = _digest(source_binding_sha256, label="source_binding_sha256")
    completion = _digest(completion_contract_sha256, label="completion_contract_sha256")
    if completion != ENGINEER_WORK_ITEM_COMPLETION_CONTRACT_SHA256:
        raise EngineerWorkItemContractError("completion contract is not EngineerWorkItem v1")
    key = _idempotency_key(idempotency_key)
    command = _digest(command_digest, label="command_digest")
    identifier = _item_id(work_item_id) if work_item_id is not None else new_engineer_work_item_id()
    timestamp = _now(now)
    expiry = (
        _instant(expires_at, label="expires_at")
        if expires_at is not None
        else (datetime.fromisoformat(timestamp) + timedelta(hours=ENGINEER_WORK_ITEM_DEFAULT_TTL_HOURS))
        .astimezone(UTC)
        .isoformat(timespec="seconds")
    )
    delta = datetime.fromisoformat(expiry) - datetime.fromisoformat(timestamp)
    if not timedelta(0) < delta <= timedelta(seconds=ENGINEER_WORK_ITEM_MAX_TTL_SECONDS):
        raise EngineerWorkItemContractError("work item TTL is outside the closed limit")

    replay = conn.execute(
        """SELECT item.id,item.owner_id,item.tenant_id,item.conversation_id,item.channel,
                  item.source_binding_sha256,item.completion_contract_sha256,
                  step.ordinal,step.command_digest
             FROM engineer_work_item_steps AS step
             JOIN engineer_work_items AS item ON item.id=step.work_item_id
            WHERE step.owner_id=? AND step.idempotency_key=?""",
        (owner, key),
    ).fetchone()
    if replay is not None:
        if (
            str(replay["owner_id"]) != owner
            or str(replay["tenant_id"]) != tenant
            or str(replay["conversation_id"]) != conversation
            or str(replay["channel"]) != channel.value
            or str(replay["source_binding_sha256"]) != source
            or str(replay["completion_contract_sha256"]) != completion
            or int(replay["ordinal"]) != 1
            or str(replay["command_digest"]) != command
            or (work_item_id is not None and str(replay["id"]) != identifier)
        ):
            raise EngineerWorkItemConflictError("idempotency key is already bound")
        loaded = _fetch_item(
            conn,
            work_item_id=str(replay["id"]),
            owner_id=owner,
            tenant_id=tenant,
            conversation_id=conversation,
            channel=channel,
        )
        if loaded is None:  # pragma: no cover - same-query invariant
            raise EngineerWorkItemConflictError("idempotency replay lost its item")
        return loaded
    if _retired_command_identity_exists(
        conn,
        owner_id=owner,
        idempotency_key=key,
        source_binding_sha256=source,
    ):
        raise EngineerWorkItemConflictError("command source or idempotency key is permanently fenced")

    with _mutation(conn):
        try:
            conn.execute(
                """INSERT INTO engineer_work_items(
                       id,owner_id,tenant_id,conversation_id,channel,
                       source_binding_sha256,state,revision,step_ordinal,transition,
                       completion_contract_sha256,created_at,updated_at,expires_at,
                       completed_at,closed_at
                   ) VALUES(?,?,?,?,?,?,'active',1,1,'created',?,?,?,?,NULL,NULL)""",
                (
                    identifier,
                    owner,
                    tenant,
                    conversation,
                    channel.value,
                    source,
                    completion,
                    timestamp,
                    timestamp,
                    expiry,
                ),
            )
            conn.execute(
                """INSERT INTO engineer_work_item_steps(
                       work_item_id,owner_id,ordinal,source_binding_sha256,
                       state,idempotency_key,command_digest,
                       created_at,updated_at
                   ) VALUES(?,?,1,?,'prepared',?,?,?,?)""",
                (identifier, owner, source, key, command, timestamp, timestamp),
            )
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if (
                "engineer_work_item_identity_collision" in message
                or "uq_engineer_work_items_open_scope" in message
                or "UNIQUE constraint failed" in message
            ):
                raise EngineerWorkItemConflictError(
                    "one open Engineer Work Item already owns the scope"
                ) from exc
            raise EngineerWorkItemAnchorError("Engineer Work Item scope is not owned and exact") from exc
    loaded = _fetch_item(
        conn,
        work_item_id=identifier,
        owner_id=owner,
        tenant_id=tenant,
        conversation_id=conversation,
        channel=channel,
    )
    if loaded is None:  # pragma: no cover - committed insertion invariant
        raise EngineerWorkItemConflictError("created Engineer Work Item is unavailable")
    return loaded


def _required_item(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    owner_id: str,
    tenant_id: str,
    conversation_id: str,
    channel: EngineerWorkItemChannel,
    expected_revision: int,
) -> EngineerWorkItem:
    expected_revision = _expected_revision(expected_revision)
    item = get_engineer_work_item_in_transaction(
        conn,
        work_item_id=work_item_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
    )
    if item is None or item.revision != expected_revision:
        raise EngineerWorkItemConflictError("Engineer Work Item revision is no longer current")
    return item


def bind_engineer_command_receipts_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    owner_id: str,
    tenant_id: str,
    conversation_id: str,
    channel: EngineerWorkItemChannel,
    expected_revision: int,
    ledger_binding: Mapping[str, object],
    now: str | None = None,
) -> EngineerWorkItem:
    expected_revision = _expected_revision(expected_revision)
    observed = _observed_command_ledger_binding(ledger_binding)
    current = get_engineer_work_item_in_transaction(
        conn,
        work_item_id=work_item_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
    )
    if current is None:
        raise EngineerWorkItemConflictError("Engineer Work Item is unavailable")
    if (
        observed.actor_id != current.owner_id
        or observed.tenant_id != current.tenant_id
        or observed.conversation_id != current.conversation_id
        or observed.channel is not current.channel
        or observed.source_binding_sha256 != current.current_step.source_binding_sha256
        or observed.idempotency_key != current.current_step.idempotency_key
        or observed.command_digest != current.current_step.command_digest
    ):
        raise EngineerWorkItemConflictError("command ledger binding does not match prepared work")
    job = engineer_job_receipt_sha256(
        owner_id=observed.actor_id,
        tenant_id=observed.tenant_id,
        conversation_id=observed.conversation_id,
        channel=observed.channel,
        source_binding_sha256=observed.source_binding_sha256,
        delivery_chat_id=observed.delivery_chat_id,
        idempotency_key=observed.idempotency_key,
        command_digest=observed.command_digest,
        job_id=observed.job_id,
    )
    if (
        current is not None
        and current.revision == expected_revision + 1
        and current.state is EngineerWorkItemState.WAITING_FOR_CAPABILITY
        and current.current_step.command_digest == observed.command_digest
        and current.current_step.job_receipt_sha256 == job
    ):
        return current
    item = _required_item(
        conn,
        work_item_id=work_item_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
        expected_revision=expected_revision,
    )
    if item.state is not EngineerWorkItemState.ACTIVE:
        raise EngineerWorkItemConflictError("only a prepared step can admit a command")
    timestamp = _now(now)
    # This mutation records an already-admitted external job; it is also the
    # crash-recovery seam after a healthy exact key+digest lookup.  The item TTL
    # must not erase effect truth or make a late reconciliation impossible.
    with _mutation(conn):
        step = conn.execute(
            """UPDATE engineer_work_item_steps
                  SET state='admitted',job_receipt_sha256=?,
                      updated_at=?,admitted_at=?
                WHERE work_item_id=? AND ordinal=? AND state='prepared'
                  AND command_digest=?""",
            (
                job,
                timestamp,
                timestamp,
                item.id,
                item.step_ordinal,
                observed.command_digest,
            ),
        )
        updated = conn.execute(
            """UPDATE engineer_work_items
                  SET state='waiting_for_capability',revision=revision+1,
                      transition='command_admitted',updated_at=?
                WHERE id=? AND owner_id=? AND tenant_id=? AND conversation_id=? AND channel=?
                  AND state='active' AND revision=?""",
            (
                timestamp,
                item.id,
                item.owner_id,
                item.tenant_id,
                item.conversation_id,
                item.channel.value,
                expected_revision,
            ),
        )
        if step.rowcount != 1 or updated.rowcount != 1:
            raise EngineerWorkItemConflictError("command admission lost its CAS race")
    return get_engineer_work_item_in_transaction(
        conn,
        work_item_id=item.id,
        owner_id=item.owner_id,
        tenant_id=item.tenant_id,
        conversation_id=item.conversation_id,
        channel=item.channel,
    )  # type: ignore[return-value]


def mark_engineer_command_unknown_in_transaction(
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
    expected_revision = _expected_revision(expected_revision)
    current = get_engineer_work_item_in_transaction(
        conn,
        work_item_id=work_item_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
    )
    if (
        current is not None
        and current.revision == expected_revision + 1
        and current.state is EngineerWorkItemState.UNCERTAIN
    ):
        return current
    item = _required_item(
        conn,
        work_item_id=work_item_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
        expected_revision=expected_revision,
    )
    if item.state is not EngineerWorkItemState.WAITING_FOR_CAPABILITY:
        raise EngineerWorkItemConflictError("UNKNOWN can only follow admitted command work")
    timestamp = _now(now)
    with _mutation(conn):
        step = conn.execute(
            """UPDATE engineer_work_item_steps SET state='unknown',updated_at=?
                 WHERE work_item_id=? AND ordinal=? AND state='admitted'""",
            (timestamp, item.id, item.step_ordinal),
        )
        updated = conn.execute(
            """UPDATE engineer_work_items
                  SET state='uncertain',revision=revision+1,
                      transition='command_unknown',updated_at=?
                WHERE id=? AND owner_id=? AND tenant_id=? AND conversation_id=? AND channel=?
                  AND state='waiting_for_capability' AND revision=?""",
            (
                timestamp,
                item.id,
                item.owner_id,
                item.tenant_id,
                item.conversation_id,
                item.channel.value,
                expected_revision,
            ),
        )
        if step.rowcount != 1 or updated.rowcount != 1:
            raise EngineerWorkItemConflictError("UNKNOWN transition lost its CAS race")
    return get_engineer_work_item_in_transaction(
        conn,
        work_item_id=item.id,
        owner_id=item.owner_id,
        tenant_id=item.tenant_id,
        conversation_id=item.conversation_id,
        channel=item.channel,
    )  # type: ignore[return-value]


def settle_engineer_terminal_receipt_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    owner_id: str,
    tenant_id: str,
    conversation_id: str,
    channel: EngineerWorkItemChannel,
    expected_revision: int,
    verified_terminal_receipt_sha256: str,
    now: str | None = None,
) -> EngineerWorkItem:
    """Bind only the digest of a caller-verified public command receipt.

    Receipt authenticity/MAC and terminal status belong to the command kernel;
    this body-free main-DB store deliberately cannot copy or reinterpret it.
    """

    expected_revision = _expected_revision(expected_revision)
    terminal = _digest(
        verified_terminal_receipt_sha256,
        label="verified_terminal_receipt_sha256",
    )
    current = get_engineer_work_item_in_transaction(
        conn,
        work_item_id=work_item_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
    )
    if (
        current is not None
        and current.revision == expected_revision + 1
        and current.state is EngineerWorkItemState.WAITING_FOR_INPUT
        and current.current_step.terminal_receipt_sha256 == terminal
    ):
        return current
    if (
        current is not None
        and current.revision == expected_revision + 2
        and current.state is EngineerWorkItemState.CANCELLED
        and current.current_step.terminal_receipt_sha256 == terminal
    ):
        return current
    item = _required_item(
        conn,
        work_item_id=work_item_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
        expected_revision=expected_revision,
    )
    if item.state not in {
        EngineerWorkItemState.WAITING_FOR_CAPABILITY,
        EngineerWorkItemState.UNCERTAIN,
    }:
        raise EngineerWorkItemConflictError("terminal receipt cannot settle this state")
    timestamp = _now(now)
    scope_live = _scope_is_live(conn, item)
    with _mutation(conn):
        step = conn.execute(
            """UPDATE engineer_work_item_steps
                  SET state='settled',terminal_receipt_sha256=?,updated_at=?,settled_at=?
                WHERE work_item_id=? AND ordinal=? AND state=?""",
            (
                terminal,
                timestamp,
                timestamp,
                item.id,
                item.step_ordinal,
                item.current_step.state.value,
            ),
        )
        updated = conn.execute(
            """UPDATE engineer_work_items
                  SET state='waiting_for_input',revision=revision+1,
                      transition='terminal_observed',updated_at=?
                WHERE id=? AND owner_id=? AND tenant_id=? AND conversation_id=? AND channel=?
                  AND state=? AND revision=?""",
            (
                timestamp,
                item.id,
                item.owner_id,
                item.tenant_id,
                item.conversation_id,
                item.channel.value,
                item.state.value,
                expected_revision,
            ),
        )
        if step.rowcount != 1 or updated.rowcount != 1:
            raise EngineerWorkItemConflictError("terminal reconciliation lost its CAS race")
        if not scope_live:
            retired = conn.execute(
                """UPDATE engineer_work_items
                      SET state='cancelled',revision=revision+1,transition='cancelled',
                          updated_at=?,closed_at=?
                    WHERE id=? AND owner_id=? AND tenant_id=? AND conversation_id=? AND channel=?
                      AND state='waiting_for_input' AND revision=?""",
                (
                    timestamp,
                    timestamp,
                    item.id,
                    item.owner_id,
                    item.tenant_id,
                    item.conversation_id,
                    item.channel.value,
                    expected_revision + 1,
                ),
            )
            if retired.rowcount != 1:
                raise EngineerWorkItemConflictError("inactive-scope retirement lost its CAS race")
    return get_engineer_work_item_in_transaction(
        conn,
        work_item_id=item.id,
        owner_id=item.owner_id,
        tenant_id=item.tenant_id,
        conversation_id=item.conversation_id,
        channel=item.channel,
    )  # type: ignore[return-value]


def start_next_engineer_step_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    owner_id: str,
    tenant_id: str,
    conversation_id: str,
    channel: EngineerWorkItemChannel,
    expected_revision: int,
    source_binding_sha256: str,
    idempotency_key: str,
    command_digest: str,
    now: str | None = None,
) -> EngineerWorkItem:
    expected_revision = _expected_revision(expected_revision)
    source = _digest(source_binding_sha256, label="source_binding_sha256")
    key = _idempotency_key(idempotency_key)
    command = _digest(command_digest, label="command_digest")
    current = get_engineer_work_item_in_transaction(
        conn,
        work_item_id=work_item_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
    )
    if current is not None:
        existing = next(
            (
                step
                for step in current.steps
                if step.idempotency_key == key or step.source_binding_sha256 == source
            ),
            None,
        )
        if existing is not None:
            if (
                current.revision == expected_revision + 1
                and current.state is EngineerWorkItemState.ACTIVE
                and current.transition is EngineerWorkItemTransition.NEXT_STEP_STARTED
                and existing.ordinal == current.step_ordinal
                and existing.ordinal >= 2
                and existing.source_binding_sha256 == source
                and existing.idempotency_key == key
                and existing.command_digest == command
                and _scope_is_live(conn, current)
            ):
                return current
            raise EngineerWorkItemConflictError("command source or idempotency key is bound")
    if _retired_command_identity_exists(
        conn,
        owner_id=_identity(owner_id, label="owner_id"),
        idempotency_key=key,
        source_binding_sha256=source,
    ):
        raise EngineerWorkItemConflictError("command source or idempotency key is permanently fenced")
    item = _required_item(
        conn,
        work_item_id=work_item_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
        expected_revision=expected_revision,
    )
    if item.state is not EngineerWorkItemState.WAITING_FOR_INPUT:
        raise EngineerWorkItemConflictError("next step requires an observed terminal receipt")
    if not _scope_is_live(conn, item):
        raise EngineerWorkItemConflictError("inactive conversation cannot start another step")
    if item.step_ordinal >= ENGINEER_WORK_ITEM_MAX_STEPS:
        raise EngineerWorkItemConflictError("Engineer Work Item exhausted its step limit")
    timestamp = _now(now)
    if timestamp >= item.expires_at:
        raise EngineerWorkItemConflictError("Engineer Work Item expired before replan")
    next_ordinal = item.step_ordinal + 1
    with _mutation(conn):
        updated = conn.execute(
            """UPDATE engineer_work_items
                  SET state='active',revision=revision+1,step_ordinal=?,
                      transition='next_step_started',updated_at=?
                WHERE id=? AND owner_id=? AND tenant_id=? AND conversation_id=? AND channel=?
                  AND state='waiting_for_input' AND revision=? AND expires_at>?""",
            (
                next_ordinal,
                timestamp,
                item.id,
                item.owner_id,
                item.tenant_id,
                item.conversation_id,
                item.channel.value,
                expected_revision,
                timestamp,
            ),
        )
        if updated.rowcount != 1:
            raise EngineerWorkItemConflictError("next-step CAS lost its race")
        try:
            conn.execute(
                """INSERT INTO engineer_work_item_steps(
                       work_item_id,owner_id,ordinal,source_binding_sha256,
                       state,idempotency_key,command_digest,
                       created_at,updated_at
                   ) VALUES(?,?,?,?,'prepared',?,?,?,?)""",
                (
                    item.id,
                    item.owner_id,
                    next_ordinal,
                    source,
                    key,
                    command,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise EngineerWorkItemConflictError("next-step idempotency key is already bound") from exc
    return get_engineer_work_item_in_transaction(
        conn,
        work_item_id=item.id,
        owner_id=item.owner_id,
        tenant_id=item.tenant_id,
        conversation_id=item.conversation_id,
        channel=item.channel,
    )  # type: ignore[return-value]


def mark_engineer_work_item_ready_to_answer_in_transaction(
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
    """CAS an observed capability result through the code-owned completion gate."""

    expected_revision = _expected_revision(expected_revision)
    timestamp = _now(now)
    current = get_engineer_work_item_in_transaction(
        conn,
        work_item_id=work_item_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
    )
    if (
        current is not None
        and current.revision == expected_revision + 1
        and current.state is EngineerWorkItemState.READY_TO_ANSWER
    ):
        if timestamp >= current.expires_at:
            raise EngineerWorkItemConflictError("Engineer Work Item expired before completion gate")
        if _scope_is_live(conn, current):
            return current
    item = _required_item(
        conn,
        work_item_id=work_item_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
        expected_revision=expected_revision,
    )
    if item.state is not EngineerWorkItemState.WAITING_FOR_INPUT:
        raise EngineerWorkItemConflictError("completion gate requires an observed terminal receipt")
    if not _scope_is_live(conn, item):
        raise EngineerWorkItemConflictError("inactive conversation cannot pass the completion gate")
    if timestamp >= item.expires_at:
        raise EngineerWorkItemConflictError("Engineer Work Item expired before completion gate")
    with _mutation(conn):
        cursor = conn.execute(
            """UPDATE engineer_work_items
                  SET state='ready_to_answer',revision=revision+1,
                      transition='answer_ready',updated_at=?
                WHERE id=? AND owner_id=? AND tenant_id=? AND conversation_id=? AND channel=?
                  AND state='waiting_for_input' AND revision=? AND expires_at>?""",
            (
                timestamp,
                item.id,
                item.owner_id,
                item.tenant_id,
                item.conversation_id,
                item.channel.value,
                expected_revision,
                timestamp,
            ),
        )
        if cursor.rowcount != 1:
            raise EngineerWorkItemConflictError("completion-gate CAS lost its race")
    return get_engineer_work_item_in_transaction(
        conn,
        work_item_id=item.id,
        owner_id=item.owner_id,
        tenant_id=item.tenant_id,
        conversation_id=item.conversation_id,
        channel=item.channel,
    )  # type: ignore[return-value]


def close_engineer_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    owner_id: str,
    tenant_id: str,
    conversation_id: str,
    channel: EngineerWorkItemChannel,
    expected_revision: int,
    terminal_state: EngineerWorkItemState,
    now: str | None = None,
) -> EngineerWorkItem:
    """Close by CAS inside the caller's publication transaction.

    This dormant store never sends or stages a message itself.  The activation
    package must place the assistant row and outbound queue row in this same
    caller transaction before treating ``COMPLETED`` as answer-committed.
    """

    transitions = {
        EngineerWorkItemState.COMPLETED: EngineerWorkItemTransition.COMPLETED,
        EngineerWorkItemState.FAILED: EngineerWorkItemTransition.FAILED,
        EngineerWorkItemState.CANCELLED: EngineerWorkItemTransition.CANCELLED,
    }
    if terminal_state not in transitions:
        raise EngineerWorkItemContractError("terminal_state is not admitted")
    expected_revision = _expected_revision(expected_revision)
    current = get_engineer_work_item_in_transaction(
        conn,
        work_item_id=work_item_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
    )
    if current is not None and current.revision == expected_revision + 1 and current.state is terminal_state:
        return current
    item = _required_item(
        conn,
        work_item_id=work_item_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
        expected_revision=expected_revision,
    )
    admitted_from = (
        {EngineerWorkItemState.READY_TO_ANSWER}
        if terminal_state is EngineerWorkItemState.COMPLETED
        else {
            EngineerWorkItemState.WAITING_FOR_INPUT,
            EngineerWorkItemState.READY_TO_ANSWER,
        }
    )
    if item.state not in admitted_from:
        raise EngineerWorkItemConflictError("effect-bearing or uncertain work cannot close")
    if terminal_state is EngineerWorkItemState.COMPLETED and not _scope_is_live(conn, item):
        raise EngineerWorkItemConflictError("inactive scope cannot publish a completed answer")
    timestamp = _now(now)
    if terminal_state is EngineerWorkItemState.COMPLETED and timestamp >= item.expires_at:
        raise EngineerWorkItemConflictError("Engineer Work Item expired before answer commit")
    completed_at = timestamp if terminal_state is EngineerWorkItemState.COMPLETED else None
    with _mutation(conn):
        updated = conn.execute(
            """UPDATE engineer_work_items
                  SET state=?,revision=revision+1,transition=?,updated_at=?,
                      completed_at=?,closed_at=?
                WHERE id=? AND owner_id=? AND tenant_id=? AND conversation_id=? AND channel=?
                  AND state=? AND revision=?""",
            (
                terminal_state.value,
                transitions[terminal_state].value,
                timestamp,
                completed_at,
                timestamp,
                item.id,
                item.owner_id,
                item.tenant_id,
                item.conversation_id,
                item.channel.value,
                item.state.value,
                expected_revision,
            ),
        )
        if updated.rowcount != 1:
            raise EngineerWorkItemConflictError("terminal Work Item CAS lost its race")
    return get_engineer_work_item_in_transaction(
        conn,
        work_item_id=item.id,
        owner_id=item.owner_id,
        tenant_id=item.tenant_id,
        conversation_id=item.conversation_id,
        channel=item.channel,
    )  # type: ignore[return-value]


def _fence_matches_current_step(
    item: EngineerWorkItem,
    fence: _ObservedCommandFenceBinding,
) -> bool:
    return (
        fence.actor_id == item.owner_id
        and fence.work_item_id == item.id
        and fence.expected_revision == item.revision
        and fence.step_ordinal == item.step_ordinal
        and fence.source_binding_sha256 == item.current_step.source_binding_sha256
        and fence.idempotency_key == item.current_step.idempotency_key
        and fence.command_digest == item.current_step.command_digest
    )


def _fence_matches_initial_reservation(
    item: EngineerWorkItem,
    fence: _ObservedCommandFenceBinding,
) -> bool:
    """Match a first reservation even when main was restored behind its fence."""

    return (
        fence.actor_id == item.owner_id
        and fence.expected_revision == item.revision == 1
        and fence.step_ordinal == item.step_ordinal == 1
        and fence.source_binding_sha256 == item.current_step.source_binding_sha256
        and fence.idempotency_key == item.current_step.idempotency_key
        and fence.command_digest == item.current_step.command_digest
    )


@contextmanager
def _prepared_discard_authority(
    conn: sqlite3.Connection,
    *,
    item: EngineerWorkItem,
    fence: _ObservedCommandFenceBinding,
) -> Iterator[None]:
    expected_authority = (
        item.id,
        item.owner_id,
        fence.idempotency_key,
        fence.command_digest,
    )

    def _authorize_discard(
        candidate_id: object,
        candidate_owner: object,
        candidate_key: object,
        candidate_digest: object,
    ) -> int:
        return int((candidate_id, candidate_owner, candidate_key, candidate_digest) == expected_authority)

    conn.create_function(
        "friday_engineer_prepared_discard_authorized",
        4,
        _authorize_discard,
    )
    try:
        yield
    finally:
        register_engineer_work_item_connection_functions(conn)


def discard_unsubmitted_engineer_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    owner_id: str,
    tenant_id: str,
    conversation_id: str,
    channel: EngineerWorkItemChannel,
    fence_binding: Mapping[str, object],
) -> bool:
    """Remove only an initial prepared reservation after a durable fence commit.

    The caller MUST first durably commit and read back the exact immutable
    command-ledger fence.  The fence closes both already-in-flight and future
    admission for this owner/key.  Whole-item deletion is restricted to the
    first never-admitted step, so no earlier effect truth can be erased.
    """

    fence = _observed_command_fence_binding(fence_binding)
    identifier = _item_id(work_item_id)
    if fence.actor_id != _identity(owner_id, label="owner_id"):
        raise EngineerWorkItemConflictError("command fence belongs to another owner")
    retired = _lookup_retired_command_fence(
        conn,
        owner_id=fence.actor_id,
        idempotency_key=fence.idempotency_key,
    )
    if retired is not None and retired != fence:
        raise EngineerWorkItemConflictError("command fence identity is already retired")
    current = get_engineer_work_item_in_transaction(
        conn,
        work_item_id=identifier,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
    )
    if current is None:
        if retired == fence:
            return False
        raise EngineerWorkItemConflictError("discarded reservation has no durable main fence")
    item = _required_item(
        conn,
        work_item_id=identifier,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
        expected_revision=fence.expected_revision,
    )
    if (
        not _fence_matches_initial_reservation(item, fence)
        or item.state is not EngineerWorkItemState.ACTIVE
        or item.transition is not EngineerWorkItemTransition.CREATED
        or item.revision != 1
        or item.step_ordinal != 1
        or len(item.steps) != 1
        or item.current_step.state is not EngineerWorkItemStepState.PREPARED
    ):
        raise EngineerWorkItemConflictError("only the exact fenced initial reservation can be discarded")
    with _prepared_discard_authority(conn, item=item, fence=fence), _mutation(conn):
        _persist_retired_command_fence(
            conn,
            fence=fence,
            retired_at=_now(None),
        )
        cursor = conn.execute(
            """DELETE FROM engineer_work_items
                     WHERE id=? AND owner_id=? AND tenant_id=? AND conversation_id=? AND channel=?
                       AND state='active' AND transition='created'
                       AND revision=1 AND step_ordinal=1
                       AND EXISTS (
                           SELECT 1 FROM engineer_work_item_steps AS step
                            WHERE step.work_item_id=engineer_work_items.id
                              AND step.ordinal=1 AND step.state='prepared'
                              AND step.source_binding_sha256=?
                              AND step.idempotency_key=? AND step.command_digest=?
                       )""",
            (
                item.id,
                item.owner_id,
                item.tenant_id,
                item.conversation_id,
                item.channel.value,
                fence.source_binding_sha256,
                fence.idempotency_key,
                fence.command_digest,
            ),
        )
        if cursor.rowcount != 1:
            raise EngineerWorkItemConflictError("prepared Engineer Work Item discard lost its CAS race")
    return True


def rollback_fenced_unsubmitted_engineer_step_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    owner_id: str,
    tenant_id: str,
    conversation_id: str,
    channel: EngineerWorkItemChannel,
    fence_binding: Mapping[str, object],
    now: str | None = None,
) -> EngineerWorkItem:
    """Retire a fenced later prepared step without erasing earlier receipts."""

    fence = _observed_command_fence_binding(fence_binding)
    if fence.work_item_id != _item_id(work_item_id) or fence.actor_id != _identity(
        owner_id,
        label="owner_id",
    ):
        raise EngineerWorkItemConflictError("command fence belongs to another Work Item")
    retired = _lookup_retired_command_fence(
        conn,
        owner_id=fence.actor_id,
        idempotency_key=fence.idempotency_key,
    )
    if retired is not None and retired != fence:
        raise EngineerWorkItemConflictError("command fence identity is already retired")
    current = get_engineer_work_item_in_transaction(
        conn,
        work_item_id=work_item_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
    )
    if current is None:
        raise EngineerWorkItemConflictError("Engineer Work Item is unavailable")
    if (
        current.id == fence.work_item_id
        and current.owner_id == fence.actor_id
        and current.revision == fence.expected_revision + 1
        and current.step_ordinal == fence.step_ordinal - 1
        and current.transition is EngineerWorkItemTransition.PREPARED_STEP_DISCARDED
        and current.state is EngineerWorkItemState.WAITING_FOR_INPUT
        and all(step.idempotency_key != fence.idempotency_key for step in current.steps)
        and retired == fence
    ):
        return current
    if (
        current.id == fence.work_item_id
        and current.owner_id == fence.actor_id
        and current.revision == fence.expected_revision + 1
        and current.step_ordinal == fence.step_ordinal - 1
        and current.transition is EngineerWorkItemTransition.CANCELLED
        and current.state is EngineerWorkItemState.CANCELLED
        and all(step.idempotency_key != fence.idempotency_key for step in current.steps)
        and retired == fence
    ):
        return current
    item = _required_item(
        conn,
        work_item_id=work_item_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
        expected_revision=fence.expected_revision,
    )
    if (
        not _fence_matches_current_step(item, fence)
        or item.state is not EngineerWorkItemState.ACTIVE
        or item.transition is not EngineerWorkItemTransition.NEXT_STEP_STARTED
        or item.step_ordinal < 2
        or item.current_step.state is not EngineerWorkItemStepState.PREPARED
        or any(step.state is not EngineerWorkItemStepState.SETTLED for step in item.steps[:-1])
    ):
        raise EngineerWorkItemConflictError("only the exact fenced later step can be rolled back")
    timestamp = _now(now)
    scope_live = _scope_is_live(conn, item)
    target_state = EngineerWorkItemState.WAITING_FOR_INPUT if scope_live else EngineerWorkItemState.CANCELLED
    target_transition = (
        EngineerWorkItemTransition.PREPARED_STEP_DISCARDED
        if scope_live
        else EngineerWorkItemTransition.CANCELLED
    )
    with _prepared_discard_authority(conn, item=item, fence=fence), _mutation(conn):
        _persist_retired_command_fence(
            conn,
            fence=fence,
            retired_at=timestamp,
        )
        updated = conn.execute(
            """UPDATE engineer_work_items
                      SET state=?,revision=revision+1,step_ordinal=step_ordinal-1,
                          transition=?,updated_at=?,closed_at=?
                    WHERE id=? AND owner_id=? AND tenant_id=? AND conversation_id=? AND channel=?
                      AND state='active' AND transition='next_step_started'
                      AND revision=? AND step_ordinal=?""",
            (
                target_state.value,
                target_transition.value,
                timestamp,
                None if scope_live else timestamp,
                item.id,
                item.owner_id,
                item.tenant_id,
                item.conversation_id,
                item.channel.value,
                fence.expected_revision,
                fence.step_ordinal,
            ),
        )
        deleted = conn.execute(
            """DELETE FROM engineer_work_item_steps
                     WHERE work_item_id=? AND owner_id=? AND ordinal=?
                       AND source_binding_sha256=? AND state='prepared'
                       AND idempotency_key=? AND command_digest=?""",
            (
                item.id,
                item.owner_id,
                fence.step_ordinal,
                fence.source_binding_sha256,
                fence.idempotency_key,
                fence.command_digest,
            ),
        )
        if updated.rowcount != 1 or deleted.rowcount != 1:
            raise EngineerWorkItemConflictError("prepared-step rollback lost its CAS race")
    return get_engineer_work_item_in_transaction(
        conn,
        work_item_id=item.id,
        owner_id=item.owner_id,
        tenant_id=item.tenant_id,
        conversation_id=item.conversation_id,
        channel=item.channel,
    )  # type: ignore[return-value]


def expire_due_engineer_work_items_in_transaction(
    conn: sqlite3.Connection,
    *,
    now: str | None = None,
    limit: int = ENGINEER_WORK_ITEM_RETENTION_BATCH_MAX,
) -> int:
    """Expire only settled states; prepared/admitted/UNKNOWN work is preserved."""

    timestamp = _now(now)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise EngineerWorkItemContractError("expiry limit is outside the closed bound")
    rows = conn.execute(
        """SELECT id,revision,state FROM engineer_work_items
             WHERE state IN ('waiting_for_input','ready_to_answer') AND expires_at<=?
             ORDER BY expires_at,id LIMIT ?""",
        (timestamp, limit),
    ).fetchall()
    expired = 0
    with _mutation(conn):
        for row in rows:
            cursor = conn.execute(
                """UPDATE engineer_work_items
                      SET state='expired',revision=revision+1,transition='expired',
                          updated_at=?,closed_at=?
                    WHERE id=? AND state=? AND revision=?""",
                (timestamp, timestamp, row["id"], row["state"], row["revision"]),
            )
            expired += max(0, int(cursor.rowcount))
    return expired


def delete_engineer_work_item_in_transaction(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    owner_id: str,
    tenant_id: str,
    conversation_id: str,
    channel: EngineerWorkItemChannel,
    expected_revision: int,
) -> bool:
    item = _required_item(
        conn,
        work_item_id=work_item_id,
        owner_id=owner_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
        expected_revision=expected_revision,
    )
    if item.state.is_open:
        raise EngineerWorkItemConflictError("open Engineer Work Item cannot be deleted")
    with _mutation(conn):
        cursor = conn.execute(
            """DELETE FROM engineer_work_items
                 WHERE id=? AND owner_id=? AND tenant_id=? AND conversation_id=? AND channel=?
                   AND revision=? AND state=?""",
            (
                item.id,
                item.owner_id,
                item.tenant_id,
                item.conversation_id,
                item.channel.value,
                expected_revision,
                item.state.value,
            ),
        )
        if cursor.rowcount != 1:
            raise EngineerWorkItemConflictError("Engineer Work Item deletion lost its CAS race")
    return True


def prune_engineer_work_items_in_transaction(
    conn: sqlite3.Connection,
    *,
    before: str,
    limit: int = ENGINEER_WORK_ITEM_RETENTION_BATCH_MAX,
) -> int:
    cutoff = _instant(before, label="before")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise EngineerWorkItemContractError("retention limit is outside the closed bound")
    with _mutation(conn):
        cursor = conn.execute(
            """DELETE FROM engineer_work_items
                 WHERE id IN (
                     SELECT id FROM engineer_work_items
                      WHERE state IN ('completed','failed','cancelled','expired')
                        AND closed_at<?
                      ORDER BY closed_at,id LIMIT ?
                 )""",
            (cutoff, limit),
        )
    return max(0, int(cursor.rowcount))


__all__ = [
    "ENGINEER_JOB_BINDING_SCHEMA",
    "ENGINEER_SOURCE_BINDING_SCHEMA",
    "ENGINEER_WORK_ITEM_DEFAULT_TTL_HOURS",
    "ENGINEER_WORK_ITEM_RETENTION_BATCH_MAX",
    "ENGINEER_WORK_ITEM_RETENTION_DAYS",
    "ENGINEER_WORK_ITEM_CONTRACT_SCHEMA",
    "ENGINEER_WORK_ITEM_STEP_SCHEMA",
    "EngineerWorkItem",
    "EngineerWorkItemAnchorError",
    "EngineerWorkItemChannel",
    "EngineerWorkItemConflictError",
    "EngineerWorkItemContractError",
    "EngineerWorkItemState",
    "EngineerWorkItemStep",
    "EngineerWorkItemStepState",
    "EngineerWorkItemTransition",
    "bind_engineer_command_receipts_in_transaction",
    "close_engineer_work_item_in_transaction",
    "create_engineer_work_item_in_transaction",
    "delete_engineer_work_item_in_transaction",
    "discard_unsubmitted_engineer_work_item_in_transaction",
    "engineer_source_binding_sha256",
    "engineer_job_receipt_sha256",
    "expire_due_engineer_work_items_in_transaction",
    "get_current_engineer_work_item_in_transaction",
    "get_engineer_work_item_in_transaction",
    "mark_engineer_command_unknown_in_transaction",
    "mark_engineer_work_item_ready_to_answer_in_transaction",
    "new_engineer_work_item_id",
    "prune_engineer_work_items_in_transaction",
    "rollback_fenced_unsubmitted_engineer_step_in_transaction",
    "settle_engineer_terminal_receipt_in_transaction",
    "start_next_engineer_step_in_transaction",
]
