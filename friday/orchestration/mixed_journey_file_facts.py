"""Read-only, body-free facts for a mixed-journey file organ.

The file identifier, digest, and optional MIME type are supplied by an
upstream observer.  This adapter never opens a path, hashes bytes, or keeps a
filename.  Suspicious or malformed input is converted to a closed BLOCKED
result with no path-like value in its output.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

MIXED_JOURNEY_FILE_FACTS_SCHEMA = "friday.mixed-journey-file-facts.v1"
MAX_FILE_ID_CHARS = 128
MAX_MIME_TYPE_CHARS = 128
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MIME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,62}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,62}\Z")
_PRIVATE_NAME_RE = re.compile(
    r"(?i)(?:^|[_.-])(?:secret|private|password|passwd|credential|token|api[-_]?key|\.env)(?:$|[_.-])"
)
_MISSING = object()


class MixedJourneyFileFactsError(ValueError):
    """A file fact or serialized result is malformed."""


class MixedJourneyFileFactsState(StrEnum):
    EMPTY = "empty"
    PRESENT = "present"
    BLOCKED = "blocked"


class MixedJourneyFileFactsReason(StrEnum):
    NO_FACTS = "no_facts"
    PRESENT = "present"
    INVALID_FACTS = "invalid_facts"
    INVALID_FILE_ID = "invalid_file_id"
    INVALID_DIGEST = "invalid_digest"
    INVALID_MIME_TYPE = "invalid_mime_type"
    PRIVATE_FACT = "private_fact"


@dataclass(frozen=True, slots=True)
class MixedJourneyFileFactsInputV1:
    """Facts supplied by an observer without retaining file bytes or paths."""

    file_id: str | None = None
    sha256: str | None = None
    mime_type: str | None = None

    @property
    def digest(self) -> str | None:
        return self.sha256


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise MixedJourneyFileFactsError(f"{field}_{detail}")


def _id(value: object, *, field: str = "file_id") -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    if _PRIVATE_NAME_RE.search(cast(str, value)):
        _fail(field, "private")
    return cast(str, value)


def _sha256(value: object) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _fail("sha256", "hex")
    return cast(str, value)


def _mime(value: object) -> str:
    if type(value) is not str or not value or len(value) > MAX_MIME_TYPE_CHARS:
        _fail("mime_type")
    if _MIME_RE.fullmatch(value) is None:
        _fail("mime_type")
    return value


def _reason(value: object) -> MixedJourneyFileFactsReason:
    if isinstance(value, MixedJourneyFileFactsReason):
        return value
    try:
        return MixedJourneyFileFactsReason(str(value).strip().casefold())
    except (TypeError, ValueError) as exc:
        raise MixedJourneyFileFactsError("reason_closed") from exc


@dataclass(frozen=True, slots=True)
class MixedJourneyFileFactsV1:
    """One immutable file-organ result."""

    file_id: str | None
    state: MixedJourneyFileFactsState
    sha256: str | None
    mime_type: str | None
    reason: MixedJourneyFileFactsReason

    def __post_init__(self) -> None:
        if self.file_id is not None:
            _id(self.file_id)
        try:
            state = MixedJourneyFileFactsState(self.state)
            reason = MixedJourneyFileFactsReason(self.reason)
        except (TypeError, ValueError) as exc:
            raise MixedJourneyFileFactsError("state_closed") from exc
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)
        if state is MixedJourneyFileFactsState.PRESENT:
            if self.file_id is None:
                _fail("file_id")
            _sha256(self.sha256)
            if self.mime_type is not None:
                _mime(self.mime_type)
        elif self.sha256 is not None or self.mime_type is not None:
            _fail("non_present", "leak")
        if state is MixedJourneyFileFactsState.BLOCKED and self.file_id is not None:
            _fail("blocked", "path_leak")

    @property
    def fact_state(self) -> MixedJourneyFileFactsState:
        return self.state

    @property
    def file_state(self) -> MixedJourneyFileFactsState:
        return self.state

    @property
    def decision(self) -> MixedJourneyFileFactsState:
        return self.state

    @property
    def digest(self) -> str | None:
        return self.sha256

    @property
    def file_sha256(self) -> str | None:
        return self.sha256

    @property
    def summary_digest(self) -> str | None:
        return self.sha256

    @property
    def closed_reason(self) -> MixedJourneyFileFactsReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": MIXED_JOURNEY_FILE_FACTS_SCHEMA,
            "file_id": self.file_id,
            "state": self.state.value,
            "sha256": self.sha256,
            "mime_type": self.mime_type,
            "reason": self.reason.value,
        }


FileFactsState = MixedJourneyFileFactsState
FileFactsReason = MixedJourneyFileFactsReason
FileFactsInput = MixedJourneyFileFactsInputV1
MixedJourneyFileFacts = MixedJourneyFileFactsV1


def _blocked() -> MixedJourneyFileFactsV1:
    return MixedJourneyFileFactsV1(
        None, MixedJourneyFileFactsState.BLOCKED, None, None, MixedJourneyFileFactsReason.INVALID_FACTS
    )


def _known(raw: Mapping[str, object]) -> None:
    allowed = {
        "schema",
        "file_id",
        "id",
        "state",
        "reason",
        "sha256",
        "digest",
        "file_sha256",
        "mime_type",
        "mime",
    }
    if set(raw) - allowed:
        _fail("facts", "unknown")
    if raw.get("schema", MIXED_JOURNEY_FILE_FACTS_SCHEMA) != MIXED_JOURNEY_FILE_FACTS_SCHEMA:
        _fail("schema")


def build_mixed_journey_file_facts(
    file_id: str | Mapping[str, object] | None = None,
    sha256: object = _MISSING,
    mime_type: object = _MISSING,
    *,
    digest: object = _MISSING,
    file_sha256: object = _MISSING,
    mime: object = _MISSING,
    facts: MixedJourneyFileFactsInputV1 | Mapping[str, object] | None = None,
) -> MixedJourneyFileFactsV1:
    """Validate already-supplied file metadata and fail closed."""

    if facts is not None:
        if file_id is not None or any(
            value is not _MISSING for value in (sha256, mime_type, digest, file_sha256, mime)
        ):
            return _blocked()
        if isinstance(facts, MixedJourneyFileFactsInputV1):
            file_id, sha256, mime_type = facts.file_id, facts.sha256, facts.mime_type
        elif isinstance(facts, Mapping):
            file_id = facts
        else:
            return _blocked()
    if digest is not _MISSING and sha256 is not _MISSING:
        return _blocked()
    if file_sha256 is not _MISSING:
        if sha256 is not _MISSING or digest is not _MISSING:
            return _blocked()
        sha256 = file_sha256
    if mime is not _MISSING:
        if mime_type is not _MISSING:
            return _blocked()
        mime_type = mime
    if digest is not _MISSING:
        sha256 = digest
    if isinstance(file_id, Mapping):
        raw = file_id
        try:
            _known(raw)
            raw_id = raw.get("file_id", raw.get("id"))
            state = raw.get("state")
            if state in {"empty", "blocked"}:
                selected = MixedJourneyFileFactsState(state)
                return MixedJourneyFileFactsV1(
                    None if selected is MixedJourneyFileFactsState.BLOCKED else _id(raw_id),
                    selected,
                    None,
                    None,
                    _reason(raw.get("reason", "no_facts")),
                )
            if sha256 is not _MISSING or mime_type is not _MISSING or digest is not _MISSING:
                _fail("facts", "duplicate")
            file_id = cast(str | None, raw_id)
            sha256 = raw.get("sha256", raw.get("file_sha256", raw.get("digest")))
            mime_type = raw.get("mime_type", raw.get("mime"))
        except (TypeError, ValueError, MixedJourneyFileFactsError):
            return _blocked()
    if file_id is None:
        return MixedJourneyFileFactsV1(
            None, MixedJourneyFileFactsState.EMPTY, None, None, MixedJourneyFileFactsReason.NO_FACTS
        )
    try:
        key = _id(file_id)
    except MixedJourneyFileFactsError as exc:
        reason = (
            MixedJourneyFileFactsReason.PRIVATE_FACT
            if "private" in str(exc)
            else MixedJourneyFileFactsReason.INVALID_FILE_ID
        )
        return MixedJourneyFileFactsV1(None, MixedJourneyFileFactsState.BLOCKED, None, None, reason)
    if sha256 is _MISSING:
        sha256 = None
    if mime_type is _MISSING:
        mime_type = None
    if sha256 in (None, "") and mime_type is None:
        return MixedJourneyFileFactsV1(
            key, MixedJourneyFileFactsState.EMPTY, None, None, MixedJourneyFileFactsReason.NO_FACTS
        )
    try:
        digest_value = _sha256(sha256)
        mime_value = None if mime_type is None else _mime(mime_type)
    except MixedJourneyFileFactsError as exc:
        reason = (
            MixedJourneyFileFactsReason.INVALID_MIME_TYPE
            if "mime_type" in str(exc)
            else MixedJourneyFileFactsReason.INVALID_DIGEST
        )
        return MixedJourneyFileFactsV1(None, MixedJourneyFileFactsState.BLOCKED, None, None, reason)
    return MixedJourneyFileFactsV1(
        key, MixedJourneyFileFactsState.PRESENT, digest_value, mime_value, MixedJourneyFileFactsReason.PRESENT
    )


def validate_mixed_journey_file_facts(value: object) -> bool:
    try:
        result = (
            value
            if isinstance(value, MixedJourneyFileFactsV1)
            else build_mixed_journey_file_facts(cast(Mapping[str, object], value))
        )
        return (
            isinstance(result, MixedJourneyFileFactsV1)
            and result.state is not MixedJourneyFileFactsState.BLOCKED
        )
    except (TypeError, ValueError):
        return False


build_file_facts = build_mixed_journey_file_facts
validate_file_facts = validate_mixed_journey_file_facts

__all__ = [
    "MIXED_JOURNEY_FILE_FACTS_SCHEMA",
    "FileFactsInput",
    "FileFactsReason",
    "FileFactsState",
    "MixedJourneyFileFacts",
    "MixedJourneyFileFactsError",
    "MixedJourneyFileFactsInputV1",
    "MixedJourneyFileFactsReason",
    "MixedJourneyFileFactsState",
    "MixedJourneyFileFactsV1",
    "build_file_facts",
    "build_mixed_journey_file_facts",
    "validate_file_facts",
    "validate_mixed_journey_file_facts",
]
