"""Pure SHA-256 digest facts for a coding archive.

The digest is supplied by an upstream observer.  This module validates its
shape only; it never hashes bytes, opens a path, or reads an archive.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

CODING_ARCHIVE_DIGEST_FACTS_SCHEMA = "friday.coding-archive-digest-facts.v1"
MAX_DIGEST_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class CodingArchiveDigestFactsError(ValueError):
    """A digest identity, fact, or result is malformed."""


class CodingArchiveDigestFactsState(StrEnum):
    """Closed outcomes for one supplied archive digest fact."""

    EMPTY = "empty"
    BOUND = "bound"
    BLOCKED = "blocked"


class CodingArchiveDigestFactsReason(StrEnum):
    """Closed reason for one digest result."""

    NO_DIGEST = "no_digest"
    SHA256_BOUND = "sha256_bound"
    INVALID_DIGEST = "invalid_digest"
    INVALID_FACTS = "invalid_facts"


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise CodingArchiveDigestFactsError(f"{field}_{detail}")


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _state(value: object) -> CodingArchiveDigestFactsState:
    try:
        return CodingArchiveDigestFactsState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingArchiveDigestFactsError("digest_state_closed") from exc


def _reason(value: object) -> CodingArchiveDigestFactsReason:
    try:
        return CodingArchiveDigestFactsReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingArchiveDigestFactsError("reason_closed") from exc


def _sha256(value: object, *, field: str = "sha256") -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _fail(field, "hex")
    return cast(str, value)


@dataclass(frozen=True, slots=True)
class CodingArchiveDigestInputV1:
    """Frozen digest input supplied by an external archive observer."""

    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class CodingArchiveDigestFactsV1:
    """Immutable digest binding without retaining archive bytes."""

    digest_id: str
    authenticated_turn_id: str
    digest_state: CodingArchiveDigestFactsState
    sha256: str | None
    reason: CodingArchiveDigestFactsReason

    def __post_init__(self) -> None:
        _identifier(self.digest_id, field="digest_id", maximum=MAX_DIGEST_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        state = _state(self.digest_state)
        reason = _reason(self.reason)
        object.__setattr__(self, "digest_state", state)
        object.__setattr__(self, "reason", reason)
        if state is CodingArchiveDigestFactsState.BOUND:
            _sha256(self.sha256)
        elif self.sha256 is not None:
            _fail("blocked_or_empty_digest", "exposed")

    @property
    def state(self) -> CodingArchiveDigestFactsState:
        return self.digest_state

    @property
    def closed_digest(self) -> CodingArchiveDigestFactsState:
        return self.digest_state

    @property
    def decision(self) -> CodingArchiveDigestFactsState:
        return self.digest_state

    @property
    def digest(self) -> str | None:
        return self.sha256

    @property
    def digest_sha256(self) -> str | None:
        return self.sha256

    @property
    def archive_sha256(self) -> str | None:
        return self.sha256

    @property
    def closed_reason(self) -> CodingArchiveDigestFactsReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_ARCHIVE_DIGEST_FACTS_SCHEMA,
            "digest_id": self.digest_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "digest_state": self.digest_state.value,
            "sha256": self.sha256,
            "reason": self.reason.value,
        }


DigestFactsState = CodingArchiveDigestFactsState
DigestFactsReason = CodingArchiveDigestFactsReason
CodingArchiveDigestFacts = CodingArchiveDigestFactsV1
CodingArchiveDigestState = CodingArchiveDigestFactsState
CodingArchiveDigestReason = CodingArchiveDigestFactsReason


def _input_digest(value: object) -> object:
    if isinstance(value, CodingArchiveDigestInputV1):
        return value.sha256
    if isinstance(value, Mapping):
        allowed = {"sha256", "digest_sha256", "archive_sha256", "digest"}
        if set(value) - allowed:
            _fail("digest", "unknown_fields")
        return value.get(
            "sha256",
            value.get("digest_sha256", value.get("archive_sha256", value.get("digest"))),
        )
    return value


def _result(
    digest_id: str,
    authenticated_turn_id: str,
    state: CodingArchiveDigestFactsState,
    reason: CodingArchiveDigestFactsReason,
    sha256: str | None = None,
) -> CodingArchiveDigestFactsV1:
    return CodingArchiveDigestFactsV1(
        digest_id=digest_id,
        authenticated_turn_id=authenticated_turn_id,
        digest_state=state,
        sha256=sha256,
        reason=reason,
    )


def _known_mapping_keys(raw: Mapping[str, Any]) -> None:
    known = {
        "schema",
        "digest_id",
        "authenticated_turn_id",
        "digest",
        "sha256",
        "digest_sha256",
        "archive_sha256",
        "digest_state",
        "state",
        "reason",
    }
    if set(raw) - known:
        _fail("digest", "unknown_fields")


def build_coding_archive_digest_facts(
    digest_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    digest: object = None,
) -> CodingArchiveDigestFactsV1:
    """Bind an externally supplied lowercase SHA-256 digest, if present."""

    if isinstance(digest_id, Mapping):
        raw = digest_id
        _known_mapping_keys(raw)
        if raw.get("schema", CODING_ARCHIVE_DIGEST_FACTS_SCHEMA) != CODING_ARCHIVE_DIGEST_FACTS_SCHEMA:
            _fail("schema")
        output_keys = {"digest_state", "state", "sha256", "reason"}
        fact_keys = {"digest", "digest_sha256", "archive_sha256"}
        if output_keys.intersection(raw) and fact_keys.intersection(raw):
            _fail("digest", "duplicate_representations")
        if output_keys.intersection(raw):
            return CodingArchiveDigestFactsV1(
                digest_id=cast(str, raw.get("digest_id")),
                authenticated_turn_id=cast(str, raw.get("authenticated_turn_id")),
                digest_state=cast(
                    CodingArchiveDigestFactsState,
                    raw.get("digest_state", raw.get("state")),
                ),
                sha256=cast(str | None, raw.get("sha256")),
                reason=cast(CodingArchiveDigestFactsReason, raw.get("reason")),
            )
        digest_id = cast(str, raw.get("digest_id"))
        authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
        digest = raw.get(
            "sha256",
            raw.get("digest_sha256", raw.get("archive_sha256", raw.get("digest"))),
        )

    digest_key = _identifier(digest_id, field="digest_id", maximum=MAX_DIGEST_ID_CHARS)
    turn_key = _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    try:
        digest_value = _input_digest(digest)
    except CodingArchiveDigestFactsError:
        return _result(
            digest_key,
            turn_key,
            CodingArchiveDigestFactsState.BLOCKED,
            CodingArchiveDigestFactsReason.INVALID_FACTS,
        )
    if digest_value is None or digest_value == "":
        return _result(
            digest_key,
            turn_key,
            CodingArchiveDigestFactsState.EMPTY,
            CodingArchiveDigestFactsReason.NO_DIGEST,
        )
    try:
        sha256 = _sha256(digest_value)
    except CodingArchiveDigestFactsError:
        return _result(
            digest_key,
            turn_key,
            CodingArchiveDigestFactsState.BLOCKED,
            CodingArchiveDigestFactsReason.INVALID_DIGEST,
        )
    return _result(
        digest_key,
        turn_key,
        CodingArchiveDigestFactsState.BOUND,
        CodingArchiveDigestFactsReason.SHA256_BOUND,
        sha256,
    )


def validate_coding_archive_digest_facts(value: object) -> bool:
    """Return whether a digest result or serialized result is valid."""

    try:
        if isinstance(value, CodingArchiveDigestFactsV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping):
            return False
        _known_mapping_keys(value)
        if value.get("schema") != CODING_ARCHIVE_DIGEST_FACTS_SCHEMA:
            return False
        required = {
            "schema",
            "digest_id",
            "authenticated_turn_id",
            "digest_state",
            "sha256",
            "reason",
        }
        if set(value) != required:
            return False
        return (
            CodingArchiveDigestFactsV1(
                digest_id=cast(str, value.get("digest_id")),
                authenticated_turn_id=cast(str, value.get("authenticated_turn_id")),
                digest_state=cast(CodingArchiveDigestFactsState, value.get("digest_state")),
                sha256=cast(str | None, value.get("sha256")),
                reason=cast(CodingArchiveDigestFactsReason, value.get("reason")),
            )
            is not None
        )
    except (TypeError, ValueError):
        return False


bind_coding_archive_digest = build_coding_archive_digest_facts
build_archive_digest_facts = build_coding_archive_digest_facts
validate_archive_digest_facts = validate_coding_archive_digest_facts


__all__ = [
    "CODING_ARCHIVE_DIGEST_FACTS_SCHEMA",
    "MAX_AUTHENTICATED_TURN_ID_CHARS",
    "MAX_DIGEST_ID_CHARS",
    "CodingArchiveDigestFacts",
    "CodingArchiveDigestFactsError",
    "CodingArchiveDigestFactsReason",
    "CodingArchiveDigestFactsState",
    "CodingArchiveDigestFactsV1",
    "CodingArchiveDigestInputV1",
    "CodingArchiveDigestReason",
    "CodingArchiveDigestState",
    "DigestFactsReason",
    "DigestFactsState",
    "bind_coding_archive_digest",
    "build_archive_digest_facts",
    "build_coding_archive_digest_facts",
    "validate_archive_digest_facts",
    "validate_coding_archive_digest_facts",
]
