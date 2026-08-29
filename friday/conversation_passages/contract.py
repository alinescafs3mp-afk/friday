"""Body-free contracts for the rebuildable conversation-passage sidecar.

One stored child identifies one anchor message.  Request-specific neighbouring
messages remain the responsibility of the released archive-message selector;
they are deliberately not copied into this contract or SQLite projection.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum

CONVERSATION_PASSAGE_INDEX_REVISION = "conversation-anchor-message-v1"
CONVERSATION_PASSAGE_MAX_COUNT = 2_147_483_647
CONVERSATION_PASSAGE_MAX_PAGE = 256

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{16}\Z")


def _chain_seed(domain: bytes) -> str:
    return hashlib.sha256(domain + b"\0").hexdigest()


CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256 = _chain_seed(b"friday.conversation-passage-prefix.v1")
CONVERSATION_PASSAGE_EMPTY_SET_SHA256 = _chain_seed(b"friday.conversation-passage-set.v1")


class ConversationPassageContractError(ValueError):
    """A body-free conversation-passage value is malformed."""


class ConversationPassageProjectionStatus(StrEnum):
    CURRENT = "current"
    INCOMPLETE = "incomplete"


class ConversationPassageIncompleteReason(StrEnum):
    BACKFILL_PENDING = "backfill_pending"
    SOURCE_CHANGED = "source_changed"
    SOURCE_UNAVAILABLE = "source_unavailable"


def _identifier(value: object, *, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ConversationPassageContractError(f"{label} is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ConversationPassageContractError(f"{label} is invalid") from None
    if len(encoded) > 200 or any(ord(character) < 32 for character in value):
        raise ConversationPassageContractError(f"{label} is invalid")
    return value


def _digest(value: object, *, label: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ConversationPassageContractError(f"{label} is invalid")
    return value


def _message_identifier(value: object, *, label: str) -> str:
    identifier = _identifier(value, label=label)
    if _MESSAGE_ID.fullmatch(identifier) is None:
        raise ConversationPassageContractError(f"{label} is invalid")
    return identifier


def _count(value: object, *, label: str) -> int:
    if type(value) is not int or not 0 <= value <= CONVERSATION_PASSAGE_MAX_COUNT:
        raise ConversationPassageContractError(f"{label} is invalid")
    return value


def _ordinal(value: object, *, label: str) -> int:
    if type(value) is not int or not 0 <= value < CONVERSATION_PASSAGE_MAX_COUNT:
        raise ConversationPassageContractError(f"{label} is invalid")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class ConversationPassageAnchor:
    """One exact body-free anchor row; it contains no message text."""

    conversation_id: str
    anchor_message_id: str
    anchor_ordinal: int
    anchor_message_revision_sha256: str
    anchor_content_sha256: str
    anchor_locator_sha256: str
    conversation_prefix_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.conversation_id, label="conversation identity")
        _message_identifier(self.anchor_message_id, label="anchor message identity")
        _ordinal(self.anchor_ordinal, label="anchor ordinal")
        _digest(self.anchor_message_revision_sha256, label="anchor message revision")
        _digest(self.anchor_content_sha256, label="anchor content digest")
        _digest(self.anchor_locator_sha256, label="anchor locator digest")
        _digest(self.conversation_prefix_sha256, label="conversation prefix revision")

    def __repr__(self) -> str:
        return f"ConversationPassageAnchor(anchor_ordinal={self.anchor_ordinal}, body_free=True)"


@dataclass(frozen=True, slots=True, repr=False)
class ConversationPassageProjectionRead:
    """One bounded, accepted-boundary-scoped body-free projection page.

    This is deliberately not a global sidecar-health DTO.  Every count and
    digest belongs to the immutable accepted-turn prefix; future projection
    work cannot change or gate this proof.
    """

    conversation_id: str
    passage_index_revision: str
    boundary_identity_sha256: str
    authorized_message_count: int
    authorized_projected_count: int
    authorized_projection_complete: bool
    authorized_indexed_through_message_id: str | None
    authorized_conversation_revision_sha256: str
    authorized_passage_set_sha256: str
    anchor_offset: int
    anchors: tuple[ConversationPassageAnchor, ...]
    has_more: bool

    def __post_init__(self) -> None:
        _identifier(self.conversation_id, label="conversation identity")
        _digest(self.boundary_identity_sha256, label="accepted boundary identity")
        _count(self.authorized_message_count, label="authorized message count")
        _count(self.authorized_projected_count, label="authorized projected count")
        _digest(
            self.authorized_conversation_revision_sha256,
            label="authorized conversation revision",
        )
        _digest(self.authorized_passage_set_sha256, label="authorized passage set")
        _count(self.anchor_offset, label="anchor offset")
        if self.passage_index_revision != CONVERSATION_PASSAGE_INDEX_REVISION:
            raise ConversationPassageContractError("passage index revision is invalid")
        if type(self.anchors) is not tuple:
            raise ConversationPassageContractError("projection anchors are invalid")
        expected_complete = self.authorized_projected_count == self.authorized_message_count
        expected_has_more = self.anchor_offset + len(self.anchors) < self.authorized_projected_count
        if (
            len(self.anchors) > CONVERSATION_PASSAGE_MAX_PAGE
            or any(type(item) is not ConversationPassageAnchor for item in self.anchors)
            or self.authorized_projected_count > self.authorized_message_count
            or any(
                first.anchor_ordinal >= second.anchor_ordinal
                for first, second in zip(self.anchors, self.anchors[1:], strict=False)
            )
            or any(
                item.anchor_ordinal != self.anchor_offset + index for index, item in enumerate(self.anchors)
            )
            or any(item.anchor_ordinal >= self.authorized_projected_count for item in self.anchors)
            or any(item.conversation_id != self.conversation_id for item in self.anchors)
            or len({item.anchor_message_id for item in self.anchors}) != len(self.anchors)
            or type(self.authorized_projection_complete) is not bool
            or self.authorized_projection_complete is not expected_complete
            or type(self.has_more) is not bool
            or self.anchor_offset > self.authorized_projected_count
            or self.anchor_offset + len(self.anchors) > self.authorized_projected_count
            or self.has_more is not expected_has_more
            or (self.anchor_offset < self.authorized_projected_count and not self.anchors)
        ):
            raise ConversationPassageContractError("projection anchors are invalid")

        authorized_through = (
            None
            if self.authorized_indexed_through_message_id is None
            else _message_identifier(
                self.authorized_indexed_through_message_id,
                label="authorized indexed-through message identity",
            )
        )
        if (self.authorized_projected_count == 0) is not (authorized_through is None):
            raise ConversationPassageContractError("authorized projection tail is inconsistent")
        if not self.has_more and self.anchors and (authorized_through != self.anchors[-1].anchor_message_id):
            raise ConversationPassageContractError("authorized projection tail is inconsistent")
        if self.authorized_projected_count == 0:
            if (
                self.authorized_conversation_revision_sha256 != CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256
                or self.authorized_passage_set_sha256 != CONVERSATION_PASSAGE_EMPTY_SET_SHA256
            ):
                raise ConversationPassageContractError("empty authorized projection proof is invalid")
        elif (
            not self.has_more
            and self.anchors
            and self.authorized_conversation_revision_sha256 != self.anchors[-1].conversation_prefix_sha256
        ):
            raise ConversationPassageContractError("authorized projection revision is inconsistent")

    def __repr__(self) -> str:
        return (
            "ConversationPassageProjectionRead("
            f"authorized_projected_count={self.authorized_projected_count}, "
            f"page_count={len(self.anchors)}, has_more={self.has_more}, body_free=True)"
        )


__all__ = [
    "CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256",
    "CONVERSATION_PASSAGE_EMPTY_SET_SHA256",
    "CONVERSATION_PASSAGE_INDEX_REVISION",
    "CONVERSATION_PASSAGE_MAX_COUNT",
    "CONVERSATION_PASSAGE_MAX_PAGE",
    "ConversationPassageAnchor",
    "ConversationPassageContractError",
    "ConversationPassageIncompleteReason",
    "ConversationPassageProjectionRead",
    "ConversationPassageProjectionStatus",
]
