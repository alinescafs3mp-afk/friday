"""Read-only archive facts for a mixed journey.

Only an opaque archive id, an upstream SHA-256 digest, and a bounded member
count cross this seam.  Archive bytes, paths, and member names are rejected
and are never retained in a result.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

MIXED_JOURNEY_ARCHIVE_FACTS_SCHEMA = "friday.mixed-journey-archive-facts.v1"
MAX_ARCHIVE_ID_CHARS = 128
MAX_MEMBER_COUNT = 1_000_000
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_PRIVATE_NAME_RE = re.compile(
    r"(?i)(?:^|[_.-])(?:secret|private|password|passwd|credential|token|api[-_]?key|\.env)(?:$|[_.-])"
)
_MISSING = object()


class MixedJourneyArchiveFactsError(ValueError):
    """An archive fact or serialized result is malformed."""


class MixedJourneyArchiveFactsState(StrEnum):
    EMPTY = "empty"
    PRESENT = "present"
    BLOCKED = "blocked"


class MixedJourneyArchiveFactsReason(StrEnum):
    NO_FACTS = "no_facts"
    PRESENT = "present"
    INVALID_FACTS = "invalid_facts"
    INVALID_ARCHIVE_ID = "invalid_archive_id"
    INVALID_DIGEST = "invalid_digest"
    INVALID_MEMBER_COUNT = "invalid_member_count"
    PRIVATE_FACT = "private_fact"


@dataclass(frozen=True, slots=True)
class MixedJourneyArchiveFactsInputV1:
    """Facts supplied by an archive observer, without archive contents."""

    archive_id: str | None = None
    sha256: str | None = None
    member_count: int | None = None

    @property
    def digest(self) -> str | None:
        return self.sha256


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise MixedJourneyArchiveFactsError(f"{field}_{detail}")


def _id(value: object) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail("archive_id", "id")
    if _PRIVATE_NAME_RE.search(cast(str, value)):
        _fail("archive_id", "private")
    return cast(str, value)


def _sha256(value: object) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _fail("sha256", "hex")
    return cast(str, value)


def _members(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_MEMBER_COUNT:
        _fail("member_count", "bound")
    return cast(int, value)


def _reason(value: object) -> MixedJourneyArchiveFactsReason:
    if isinstance(value, MixedJourneyArchiveFactsReason):
        return value
    try:
        return MixedJourneyArchiveFactsReason(str(value).strip().casefold())
    except (TypeError, ValueError) as exc:
        raise MixedJourneyArchiveFactsError("reason_closed") from exc


@dataclass(frozen=True, slots=True)
class MixedJourneyArchiveFactsV1:
    """One immutable archive-organ result."""

    archive_id: str | None
    state: MixedJourneyArchiveFactsState
    sha256: str | None
    member_count: int | None
    reason: MixedJourneyArchiveFactsReason

    def __post_init__(self) -> None:
        if self.archive_id is not None:
            _id(self.archive_id)
        try:
            state = MixedJourneyArchiveFactsState(self.state)
            reason = MixedJourneyArchiveFactsReason(self.reason)
        except (TypeError, ValueError) as exc:
            raise MixedJourneyArchiveFactsError("state_closed") from exc
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)
        if state is MixedJourneyArchiveFactsState.PRESENT:
            if self.archive_id is None:
                _fail("archive_id")
            _sha256(self.sha256)
            _members(self.member_count)
        elif self.sha256 is not None or self.member_count is not None:
            _fail("non_present", "leak")
        if state is MixedJourneyArchiveFactsState.BLOCKED and self.archive_id is not None:
            _fail("blocked", "name_leak")

    @property
    def fact_state(self) -> MixedJourneyArchiveFactsState:
        return self.state

    @property
    def archive_state(self) -> MixedJourneyArchiveFactsState:
        return self.state

    @property
    def decision(self) -> MixedJourneyArchiveFactsState:
        return self.state

    @property
    def digest(self) -> str | None:
        return self.sha256

    @property
    def archive_sha256(self) -> str | None:
        return self.sha256

    @property
    def summary_digest(self) -> str | None:
        return self.sha256

    @property
    def closed_reason(self) -> MixedJourneyArchiveFactsReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": MIXED_JOURNEY_ARCHIVE_FACTS_SCHEMA,
            "archive_id": self.archive_id,
            "state": self.state.value,
            "sha256": self.sha256,
            "member_count": self.member_count,
            "reason": self.reason.value,
        }


ArchiveFactsState = MixedJourneyArchiveFactsState
ArchiveFactsReason = MixedJourneyArchiveFactsReason
ArchiveFactsInput = MixedJourneyArchiveFactsInputV1
MixedJourneyArchiveFacts = MixedJourneyArchiveFactsV1


def _blocked(
    reason: MixedJourneyArchiveFactsReason = MixedJourneyArchiveFactsReason.INVALID_FACTS,
) -> MixedJourneyArchiveFactsV1:
    return MixedJourneyArchiveFactsV1(None, MixedJourneyArchiveFactsState.BLOCKED, None, None, reason)


def _known(raw: Mapping[str, object]) -> None:
    allowed = {
        "schema",
        "archive_id",
        "id",
        "state",
        "reason",
        "sha256",
        "digest",
        "archive_sha256",
        "member_count",
    }
    if set(raw) - allowed:
        _fail("facts", "unknown")
    if raw.get("schema", MIXED_JOURNEY_ARCHIVE_FACTS_SCHEMA) != MIXED_JOURNEY_ARCHIVE_FACTS_SCHEMA:
        _fail("schema")


def build_mixed_journey_archive_facts(
    archive_id: str | Mapping[str, object] | None = None,
    sha256: object = _MISSING,
    member_count: object = _MISSING,
    *,
    digest: object = _MISSING,
    archive_sha256: object = _MISSING,
    facts: MixedJourneyArchiveFactsInputV1 | Mapping[str, object] | None = None,
) -> MixedJourneyArchiveFactsV1:
    """Validate already-supplied archive metadata and fail closed."""

    if facts is not None:
        if archive_id is not None or any(
            value is not _MISSING for value in (sha256, member_count, digest, archive_sha256)
        ):
            return _blocked()
        if isinstance(facts, MixedJourneyArchiveFactsInputV1):
            archive_id, sha256, member_count = facts.archive_id, facts.sha256, facts.member_count
        elif isinstance(facts, Mapping):
            archive_id = facts
        else:
            return _blocked()
    if digest is not _MISSING and sha256 is not _MISSING:
        return _blocked()
    if archive_sha256 is not _MISSING:
        if sha256 is not _MISSING or digest is not _MISSING:
            return _blocked()
        sha256 = archive_sha256
    if digest is not _MISSING:
        sha256 = digest
    if isinstance(archive_id, Mapping):
        raw = archive_id
        try:
            _known(raw)
            raw_id = raw.get("archive_id", raw.get("id"))
            state = raw.get("state")
            if state in {"empty", "blocked"}:
                selected = MixedJourneyArchiveFactsState(state)
                return MixedJourneyArchiveFactsV1(
                    None
                    if selected is MixedJourneyArchiveFactsState.BLOCKED
                    else (None if raw_id is None else _id(raw_id)),
                    selected,
                    None,
                    None,
                    _reason(raw.get("reason", "no_facts")),
                )
            if sha256 is not _MISSING or member_count is not _MISSING or digest is not _MISSING:
                _fail("facts", "duplicate")
            archive_id = cast(str | None, raw_id)
            sha256 = raw.get("sha256", raw.get("archive_sha256", raw.get("digest")))
            member_count = raw.get("member_count")
        except (TypeError, ValueError, MixedJourneyArchiveFactsError):
            return _blocked()
    if archive_id is None:
        return MixedJourneyArchiveFactsV1(
            None, MixedJourneyArchiveFactsState.EMPTY, None, None, MixedJourneyArchiveFactsReason.NO_FACTS
        )
    try:
        key = _id(archive_id)
    except MixedJourneyArchiveFactsError as exc:
        reason = (
            MixedJourneyArchiveFactsReason.PRIVATE_FACT
            if "private" in str(exc)
            else MixedJourneyArchiveFactsReason.INVALID_ARCHIVE_ID
        )
        return _blocked(reason)
    if sha256 is _MISSING:
        sha256 = None
    if member_count is _MISSING:
        member_count = None
    if sha256 in (None, "") and member_count is None:
        return MixedJourneyArchiveFactsV1(
            key, MixedJourneyArchiveFactsState.EMPTY, None, None, MixedJourneyArchiveFactsReason.NO_FACTS
        )
    try:
        digest_value = _sha256(sha256)
        member_value = _members(member_count)
    except MixedJourneyArchiveFactsError as exc:
        reason = (
            MixedJourneyArchiveFactsReason.INVALID_MEMBER_COUNT
            if "member_count" in str(exc)
            else MixedJourneyArchiveFactsReason.INVALID_DIGEST
        )
        return _blocked(reason)
    return MixedJourneyArchiveFactsV1(
        key,
        MixedJourneyArchiveFactsState.PRESENT,
        digest_value,
        member_value,
        MixedJourneyArchiveFactsReason.PRESENT,
    )


def validate_mixed_journey_archive_facts(value: object) -> bool:
    try:
        result = (
            value
            if isinstance(value, MixedJourneyArchiveFactsV1)
            else build_mixed_journey_archive_facts(cast(Mapping[str, object], value))
        )
        return (
            isinstance(result, MixedJourneyArchiveFactsV1)
            and result.state is not MixedJourneyArchiveFactsState.BLOCKED
        )
    except (TypeError, ValueError):
        return False


build_archive_facts = build_mixed_journey_archive_facts
validate_archive_facts = validate_mixed_journey_archive_facts

__all__ = [
    "MIXED_JOURNEY_ARCHIVE_FACTS_SCHEMA",
    "ArchiveFactsInput",
    "ArchiveFactsReason",
    "ArchiveFactsState",
    "MAX_MEMBER_COUNT",
    "MixedJourneyArchiveFacts",
    "MixedJourneyArchiveFactsError",
    "MixedJourneyArchiveFactsInputV1",
    "MixedJourneyArchiveFactsReason",
    "MixedJourneyArchiveFactsState",
    "MixedJourneyArchiveFactsV1",
    "build_archive_facts",
    "build_mixed_journey_archive_facts",
    "validate_archive_facts",
    "validate_mixed_journey_archive_facts",
]
