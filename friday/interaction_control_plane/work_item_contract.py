"""Closed P2 contract for one durable ``RecallConversation`` work item.

This first vertical slice is intentionally smaller than the eventual generic
work graph.  It retains only code-owned workflow labels, a bounded temporal
frame and private durable message/outcome anchors.  Message bodies, requests,
tenant payloads and model-authored planning state cannot enter the contract.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

RECALL_CONVERSATION_ACTIVE_FRAME_SCHEMA = "friday.recall-conversation-active-frame.v1"
RECALL_CONVERSATION_WORK_ITEM_SCHEMA = "friday.recall-conversation-work-item.v1"
WORK_ITEM_ACTIVE_FRAME_MAX_BYTES = 4_096
WORK_ITEM_TTL_HOURS = 12
WORK_ITEM_MAX_REVISION = 2_147_483_647

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_WORK_ITEM_ID_RE = re.compile(r"work_[0-9a-f]{16}")
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}")
_MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{16}")
_USER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}")
EnumT = TypeVar("EnumT", bound=StrEnum)


class WorkItemContractError(ValueError):
    """A value is outside the closed RecallConversation v1 contract."""


class WorkKind(StrEnum):
    RECALL_CONVERSATION = "recall_conversation"


class WorkGoal(StrEnum):
    EXACT_CURRENT_CONVERSATION_RECALL = "exact_current_conversation_recall"


class WorkPlaybook(StrEnum):
    RECALL_CONVERSATION = "recall_conversation"


class WorkCompletionContract(StrEnum):
    ACCEPTED_EXACT_OWNED_MESSAGE_WINDOW = "accepted_exact_owned_message_window"


class WorkSourceScope(StrEnum):
    CURRENT_CONVERSATION = "current_conversation"


class RecallMessageRole(StrEnum):
    ANY = "any"
    USER = "user"
    ASSISTANT = "assistant"

    @property
    def selector_value(self) -> str | None:
        """Project the closed frame role to the legacy selector contract."""

        return None if self is RecallMessageRole.ANY else self.value


class WorkState(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class WorkTransition(StrEnum):
    CREATED = "created"
    CONSTRAINT_UPDATED = "constraint_updated"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkItemContractError("work item JSON contains a duplicate object key")
        result[key] = value
    return result


def _closed_keys(value: Mapping[Any, Any], expected: frozenset[str], *, label: str) -> None:
    if any(not isinstance(key, str) for key in value) or frozenset(value) != expected:
        raise WorkItemContractError(f"{label} keys do not match the closed contract")


def _enum_value(enum_type: type[EnumT], value: object, *, label: str) -> EnumT:
    if not isinstance(value, str) or len(value) > 64 or _contains_control(value):
        raise WorkItemContractError(f"{label} must be a closed enum value")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise WorkItemContractError(f"{label} must be a closed enum value") from exc


def _identifier(value: object, pattern: re.Pattern[str], *, label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise WorkItemContractError(f"{label} is not a valid identifier")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise WorkItemContractError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _timezone_name(value: object) -> str:
    try:
        encoded_length = len(value.encode("utf-8", errors="strict")) if isinstance(value, str) else 0
    except UnicodeEncodeError as exc:
        raise WorkItemContractError("timezone_name is not valid UTF-8") from exc
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or encoded_length > 128
        or _contains_control(value)
    ):
        raise WorkItemContractError("timezone_name is not canonical")
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise WorkItemContractError("timezone_name is not an installed IANA zone") from exc
    return value


def _utc_boundary(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 64:
        raise WorkItemContractError(f"{label} must be a canonical UTC instant")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkItemContractError(f"{label} must be a canonical UTC instant") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorkItemContractError(f"{label} must include a UTC offset")
    return parsed.astimezone(UTC).isoformat()


def canonical_work_item_instant(value: object, *, label: str) -> str:
    """Normalize lifecycle timestamps to lexically sortable UTC seconds."""

    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 64:
        raise WorkItemContractError(f"{label} must be a canonical UTC instant")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkItemContractError(f"{label} must be a canonical UTC instant") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorkItemContractError(f"{label} must include a UTC offset")
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class RecallConversationActiveFrame:
    """The only mutable semantic state in the first RecallConversation slice."""

    source_scope: WorkSourceScope
    timezone_name: str
    since_utc: str
    until_utc: str
    role: RecallMessageRole

    def __post_init__(self) -> None:
        if self.source_scope is not WorkSourceScope.CURRENT_CONVERSATION:
            raise WorkItemContractError("RecallConversation source_scope must be current_conversation")
        if not isinstance(self.role, RecallMessageRole):
            raise WorkItemContractError("role must be a RecallMessageRole")
        zone = _timezone_name(self.timezone_name)
        start = _utc_boundary(self.since_utc, label="since_utc")
        end = _utc_boundary(self.until_utc, label="until_utc")
        if zone != self.timezone_name or start != self.since_utc or end != self.until_utc:
            raise WorkItemContractError("active frame values must already be canonical")
        if start >= end:
            raise WorkItemContractError("RecallConversation time window must be non-empty")

    @classmethod
    def create(
        cls,
        *,
        timezone_name: str,
        since_utc: str,
        until_utc: str,
        role: RecallMessageRole = RecallMessageRole.ANY,
    ) -> RecallConversationActiveFrame:
        """Normalize caller inputs into the one admitted active frame."""

        return cls(
            source_scope=WorkSourceScope.CURRENT_CONVERSATION,
            timezone_name=_timezone_name(timezone_name),
            since_utc=_utc_boundary(since_utc, label="since_utc"),
            until_utc=_utc_boundary(until_utc, label="until_utc"),
            role=role,
        )

    @classmethod
    def parse(cls, value: str | Mapping[str, object]) -> RecallConversationActiveFrame:
        serialized: str | None = None
        if isinstance(value, str):
            serialized = value
            try:
                encoded = value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise WorkItemContractError("active frame JSON must be valid UTF-8") from exc
            if len(encoded) > WORK_ITEM_ACTIVE_FRAME_MAX_BYTES:
                raise WorkItemContractError("active frame JSON exceeds its closed byte limit")
            try:
                decoded = json.loads(
                    value,
                    parse_constant=lambda _value: (_ for _ in ()).throw(
                        WorkItemContractError("active frame JSON contains a non-finite number")
                    ),
                    object_pairs_hook=_closed_object,
                )
            except json.JSONDecodeError as exc:
                raise WorkItemContractError("active frame must be one JSON object") from exc
        else:
            decoded = value
        if not isinstance(decoded, Mapping):
            raise WorkItemContractError("active frame must be one JSON object")
        _closed_keys(
            decoded,
            frozenset({"schema", "source_scope", "timezone_name", "since_utc", "until_utc", "role"}),
            label="active frame",
        )
        if decoded["schema"] != RECALL_CONVERSATION_ACTIVE_FRAME_SCHEMA:
            raise WorkItemContractError("active frame schema is not supported")
        frame = cls.create(
            timezone_name=str(decoded["timezone_name"]),
            since_utc=str(decoded["since_utc"]),
            until_utc=str(decoded["until_utc"]),
            role=_enum_value(RecallMessageRole, decoded["role"], label="role"),
        )
        source_scope = _enum_value(
            WorkSourceScope,
            decoded["source_scope"],
            label="source_scope",
        )
        if source_scope is not frame.source_scope:
            raise WorkItemContractError("active frame source_scope is not admitted")
        if serialized is not None and serialized != frame.to_json():
            raise WorkItemContractError("active frame JSON is not canonical")
        return frame

    def with_time_window(self, *, since_utc: str, until_utc: str) -> RecallConversationActiveFrame:
        """Replace only the temporal bounds; preserve role, zone and source scope."""

        return RecallConversationActiveFrame.create(
            timezone_name=self.timezone_name,
            since_utc=since_utc,
            until_utc=until_utc,
            role=self.role,
        )

    def to_payload(self) -> dict[str, str]:
        return {
            "schema": RECALL_CONVERSATION_ACTIVE_FRAME_SCHEMA,
            "source_scope": self.source_scope.value,
            "timezone_name": self.timezone_name,
            "since_utc": self.since_utc,
            "until_utc": self.until_utc,
            "role": self.role.value,
        }

    def to_json(self) -> str:
        encoded = json.dumps(
            self.to_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("ascii")) > WORK_ITEM_ACTIVE_FRAME_MAX_BYTES:  # pragma: no cover
            raise WorkItemContractError("active frame JSON exceeds its closed byte limit")
        return encoded


@dataclass(frozen=True, slots=True)
class RecallConversationWorkItem:
    """Typed projection of one private durable ``work_items`` row."""

    id: str
    user_id: str
    conversation_id: str
    kind: WorkKind
    goal: WorkGoal
    state: WorkState
    playbook: WorkPlaybook
    completion_contract: WorkCompletionContract
    active_frame: RecallConversationActiveFrame
    anchor_user_message_id: str
    anchor_assistant_message_id: str
    accepted_plan_sha256: str
    accepted_outcome_sha256: str
    revision: int
    transition: WorkTransition
    created_at: str
    updated_at: str
    expires_at: str
    closed_at: str | None

    def __post_init__(self) -> None:
        _identifier(self.id, _WORK_ITEM_ID_RE, label="work_item_id")
        _identifier(self.user_id, _USER_ID_RE, label="user_id")
        _identifier(self.conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
        _identifier(self.anchor_user_message_id, _MESSAGE_ID_RE, label="anchor_user_message_id")
        _identifier(
            self.anchor_assistant_message_id,
            _MESSAGE_ID_RE,
            label="anchor_assistant_message_id",
        )
        _digest(self.accepted_plan_sha256, label="accepted_plan_sha256")
        _digest(self.accepted_outcome_sha256, label="accepted_outcome_sha256")
        for label, value, enum_type in (
            ("kind", self.kind, WorkKind),
            ("goal", self.goal, WorkGoal),
            ("state", self.state, WorkState),
            ("playbook", self.playbook, WorkPlaybook),
            ("completion_contract", self.completion_contract, WorkCompletionContract),
            ("transition", self.transition, WorkTransition),
        ):
            if not isinstance(value, enum_type):
                raise WorkItemContractError(f"{label} has an invalid enum type")
        if (
            self.kind is not WorkKind.RECALL_CONVERSATION
            or self.goal is not WorkGoal.EXACT_CURRENT_CONVERSATION_RECALL
            or self.playbook is not WorkPlaybook.RECALL_CONVERSATION
            or self.completion_contract is not WorkCompletionContract.ACCEPTED_EXACT_OWNED_MESSAGE_WINDOW
        ):
            raise WorkItemContractError("work item is outside the RecallConversation v1 slice")
        if not isinstance(self.active_frame, RecallConversationActiveFrame):
            raise WorkItemContractError("active_frame must be RecallConversationActiveFrame v1")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or not 1 <= self.revision <= WORK_ITEM_MAX_REVISION
        ):
            raise WorkItemContractError("revision is outside the closed limit")
        created = canonical_work_item_instant(self.created_at, label="created_at")
        updated = canonical_work_item_instant(self.updated_at, label="updated_at")
        expires = canonical_work_item_instant(self.expires_at, label="expires_at")
        if (created, updated, expires) != (self.created_at, self.updated_at, self.expires_at):
            raise WorkItemContractError("work item timestamps must already be canonical")
        if updated < created:
            raise WorkItemContractError("updated_at precedes created_at")
        if datetime.fromisoformat(expires) > datetime.fromisoformat(updated) + timedelta(
            hours=WORK_ITEM_TTL_HOURS
        ):
            raise WorkItemContractError("work item TTL exceeds its closed limit")
        if self.state in {WorkState.ACTIVE, WorkState.SUSPENDED}:
            if self.closed_at is not None:
                raise WorkItemContractError("open work cannot carry closed_at")
            if expires <= updated:
                raise WorkItemContractError("open work must expire after its latest update")
        else:
            if self.closed_at is None:
                raise WorkItemContractError("closed work requires closed_at")
            closed = canonical_work_item_instant(self.closed_at, label="closed_at")
            if closed != self.closed_at or closed != updated:
                raise WorkItemContractError("closed_at must equal the terminal update time")
            if self.state is WorkState.EXPIRED and expires > updated:
                raise WorkItemContractError("expired work cannot precede its expiry boundary")
        expected_transitions = {
            WorkState.ACTIVE: {WorkTransition.CREATED, WorkTransition.CONSTRAINT_UPDATED},
            WorkState.SUSPENDED: {WorkTransition.SUSPENDED},
            WorkState.CANCELLED: {WorkTransition.CANCELLED},
            WorkState.EXPIRED: {WorkTransition.EXPIRED},
        }
        if self.transition not in expected_transitions[self.state]:
            raise WorkItemContractError("transition does not match work state")
        if self.transition is WorkTransition.CREATED:
            if self.revision != 1:
                raise WorkItemContractError("created work must start at revision 1")
        elif self.revision < 2:
            raise WorkItemContractError("post-create work must have revision 2 or later")

    @classmethod
    def from_storage_row(cls, value: Mapping[str, object]) -> RecallConversationWorkItem:
        """Parse one exact ``SELECT * FROM work_items`` projection."""

        if not isinstance(value, Mapping):
            raise WorkItemContractError("work item storage row must be an object")
        _closed_keys(
            value,
            frozenset(
                {
                    "id",
                    "user_id",
                    "conversation_id",
                    "kind",
                    "goal",
                    "state",
                    "playbook",
                    "completion_contract",
                    "active_frame_json",
                    "anchor_user_message_id",
                    "anchor_assistant_message_id",
                    "accepted_plan_sha256",
                    "accepted_outcome_sha256",
                    "revision",
                    "transition",
                    "created_at",
                    "updated_at",
                    "expires_at",
                    "closed_at",
                }
            ),
            label="work item storage row",
        )
        revision = value["revision"]
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise WorkItemContractError("work item revision must be an integer")
        closed_value = value["closed_at"]
        if closed_value is not None and not isinstance(closed_value, str):
            raise WorkItemContractError("work item closed_at must be text or null")
        text_fields = {
            key: value[key]
            for key in (
                "id",
                "user_id",
                "conversation_id",
                "active_frame_json",
                "anchor_user_message_id",
                "anchor_assistant_message_id",
                "accepted_plan_sha256",
                "accepted_outcome_sha256",
                "created_at",
                "updated_at",
                "expires_at",
            )
        }
        if any(not isinstance(item, str) for item in text_fields.values()):
            raise WorkItemContractError("work item storage text columns are invalid")
        return cls(
            id=str(text_fields["id"]),
            user_id=str(text_fields["user_id"]),
            conversation_id=str(text_fields["conversation_id"]),
            kind=_enum_value(WorkKind, value["kind"], label="kind"),
            goal=_enum_value(WorkGoal, value["goal"], label="goal"),
            state=_enum_value(WorkState, value["state"], label="state"),
            playbook=_enum_value(WorkPlaybook, value["playbook"], label="playbook"),
            completion_contract=_enum_value(
                WorkCompletionContract,
                value["completion_contract"],
                label="completion_contract",
            ),
            active_frame=RecallConversationActiveFrame.parse(str(text_fields["active_frame_json"])),
            anchor_user_message_id=str(text_fields["anchor_user_message_id"]),
            anchor_assistant_message_id=str(text_fields["anchor_assistant_message_id"]),
            accepted_plan_sha256=str(text_fields["accepted_plan_sha256"]),
            accepted_outcome_sha256=str(text_fields["accepted_outcome_sha256"]),
            revision=revision,
            transition=_enum_value(WorkTransition, value["transition"], label="transition"),
            created_at=str(text_fields["created_at"]),
            updated_at=str(text_fields["updated_at"]),
            expires_at=str(text_fields["expires_at"]),
            closed_at=closed_value,
        )

    def to_payload(self) -> dict[str, object]:
        """Return the bounded private export/debug projection."""

        return {
            "schema": RECALL_CONVERSATION_WORK_ITEM_SCHEMA,
            "id": self.id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "kind": self.kind.value,
            "goal": self.goal.value,
            "state": self.state.value,
            "playbook": self.playbook.value,
            "completion_contract": self.completion_contract.value,
            "active_frame": self.active_frame.to_payload(),
            "anchor_user_message_id": self.anchor_user_message_id,
            "anchor_assistant_message_id": self.anchor_assistant_message_id,
            "accepted_plan_sha256": self.accepted_plan_sha256,
            "accepted_outcome_sha256": self.accepted_outcome_sha256,
            "revision": self.revision,
            "transition": self.transition.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "closed_at": self.closed_at,
        }


__all__ = [
    "RECALL_CONVERSATION_ACTIVE_FRAME_SCHEMA",
    "RECALL_CONVERSATION_WORK_ITEM_SCHEMA",
    "RecallConversationActiveFrame",
    "RecallConversationWorkItem",
    "RecallMessageRole",
    "WORK_ITEM_ACTIVE_FRAME_MAX_BYTES",
    "WORK_ITEM_MAX_REVISION",
    "WORK_ITEM_TTL_HOURS",
    "WorkCompletionContract",
    "WorkGoal",
    "WorkItemContractError",
    "WorkKind",
    "WorkPlaybook",
    "WorkSourceScope",
    "WorkState",
    "WorkTransition",
    "canonical_work_item_instant",
]
