"""Internal, non-user-controlled admission carried across HTTP and routing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_WORK_ITEM_ID_RE = re.compile(r"work_[0-9a-f]{16}\Z")


class PendingDurableAdmissionState(StrEnum):
    OWNED = "owned"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class PendingDurableTurnAdmission:
    """Freeze one pre-ingestion ownership decision and optional Work Item CAS."""

    state: PendingDurableAdmissionState
    person_id: str
    conversation_id: str
    work_item_id: str | None = None
    revision: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, PendingDurableAdmissionState):
            raise TypeError("pending durable admission state is invalid")
        if not isinstance(self.person_id, str) or not 1 <= len(self.person_id) <= 200:
            raise ValueError("pending durable admission person scope is invalid")
        if not isinstance(self.conversation_id, str) or not 1 <= len(self.conversation_id) <= 200:
            raise ValueError("pending durable admission conversation scope is invalid")
        bound = self.work_item_id is not None or self.revision is not None
        if self.state is PendingDurableAdmissionState.UNCERTAIN and bound:
            raise ValueError("uncertain pending durable admission cannot carry a Work Item")
        if not bound:
            return
        if (
            not isinstance(self.work_item_id, str)
            or _WORK_ITEM_ID_RE.fullmatch(self.work_item_id) is None
            or not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision < 1
        ):
            raise ValueError("pending durable admission Work Item binding is invalid")

    @classmethod
    def owned(
        cls,
        *,
        person_id: str,
        conversation_id: str,
        work_item_id: str | None = None,
        revision: int | None = None,
    ) -> PendingDurableTurnAdmission:
        return cls(
            PendingDurableAdmissionState.OWNED,
            person_id,
            conversation_id,
            work_item_id,
            revision,
        )

    @classmethod
    def uncertain(
        cls,
        *,
        person_id: str,
        conversation_id: str,
    ) -> PendingDurableTurnAdmission:
        return cls(PendingDurableAdmissionState.UNCERTAIN, person_id, conversation_id)

    @property
    def is_owned(self) -> bool:
        return self.state is PendingDurableAdmissionState.OWNED

    @property
    def is_bound(self) -> bool:
        return self.work_item_id is not None

    def matches_scope(self, *, person_id: str, conversation_id: str) -> bool:
        return self.person_id == person_id and self.conversation_id == conversation_id


__all__ = [
    "PendingDurableAdmissionState",
    "PendingDurableTurnAdmission",
]
