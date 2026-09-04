"""Pure, body-free source snapshot facts for Coding Mode.

Only relative names and lowercase SHA-256 facts cross this contract.  The
builder never opens a path or computes a digest.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

CODING_MODE_SNAPSHOT_SCHEMA = "friday.coding-mode-snapshot.v1"
MAX_SNAPSHOT_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_SNAPSHOT_MEMBERS = 32
MAX_SNAPSHOT_PATH_CHARS = 4_096

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class CodingModeSnapshotError(ValueError):
    """A snapshot identity, member name, or digest fact is malformed."""


class CodingModeSnapshotState(StrEnum):
    EMPTY = "empty"
    SNAPSHOT = "snapshot"
    BLOCKED = "blocked"


class CodingModeSnapshotReason(StrEnum):
    NO_MEMBERS = "no_members"
    SNAPSHOT_BOUND = "snapshot_bound"
    PATH_TRAVERSAL = "path_traversal"
    ABSOLUTE_PATH = "absolute_path"
    SECRET_NAME = "secret_name"
    CASEFOLD_COLLISION = "casefold_collision"
    INVALID_DIGEST = "invalid_digest"
    MEMBER_LIMIT = "member_limit"
    INVALID_FACTS = "invalid_facts"

    CASE_FOLD_COLLISION = CASEFOLD_COLLISION


def _identifier(value: object, field: str, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        raise CodingModeSnapshotError(f"{field}_id_invalid")
    return cast(str, value)


def _sha256(value: object) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise CodingModeSnapshotError("sha256_invalid")
    return cast(str, value)


def _path(value: object) -> tuple[str, str]:
    if type(value) is not str or not value or len(value) > MAX_SNAPSHOT_PATH_CHARS:
        raise CodingModeSnapshotError("relative_path_invalid")
    raw = cast(str, value)
    if raw != raw.strip() or "\x00" in raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise CodingModeSnapshotError("relative_path_invalid")
    if raw.startswith(("/", "\\")) or _DRIVE_RE.match(raw) is not None:
        raise CodingModeSnapshotError("absolute_path")
    if "\\" in raw:
        raise CodingModeSnapshotError("absolute_path")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise CodingModeSnapshotError("path_traversal")
    canonical = unicodedata.normalize("NFC", raw)
    if canonical != raw:
        raise CodingModeSnapshotError("relative_path_invalid")
    folded = canonical.casefold()
    if any(
        part.casefold() == ".env"
        or part.casefold().startswith(".env.")
        or part.casefold() in {"id_rsa", "id_ed25519", "credentials", "credential"}
        or part.casefold().startswith("secret")
        for part in parts
    ):
        raise CodingModeSnapshotError("secret_name")
    return canonical, folded


@dataclass(frozen=True, slots=True)
class CodingModeSnapshotMemberV1:
    """One relative source name and its externally supplied SHA-256."""

    relative_path: str
    sha256: str

    def __post_init__(self) -> None:
        canonical, _ = _path(self.relative_path)
        _sha256(self.sha256)
        object.__setattr__(self, "relative_path", canonical)

    @property
    def path(self) -> str:
        return self.relative_path

    @property
    def digest(self) -> str:
        return self.sha256

    @property
    def digest_sha256(self) -> str:
        return self.sha256

    def to_mapping(self) -> dict[str, Any]:
        return {"relative_path": self.relative_path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class CodingModeSnapshotV1:
    """Immutable snapshot inventory; blocked snapshots retain no names."""

    snapshot_id: str
    authenticated_turn_id: str
    snapshot: CodingModeSnapshotState
    members: tuple[CodingModeSnapshotMemberV1, ...]
    reason: CodingModeSnapshotReason

    def __post_init__(self) -> None:
        _identifier(self.snapshot_id, "snapshot_id", MAX_SNAPSHOT_ID_CHARS)
        _identifier(self.authenticated_turn_id, "authenticated_turn_id", MAX_AUTHENTICATED_TURN_ID_CHARS)
        state = _state(self.snapshot)
        reason = _reason(self.reason)
        if type(self.members) is not tuple or len(self.members) > MAX_SNAPSHOT_MEMBERS:
            raise CodingModeSnapshotError("members_invalid")
        seen: set[str] = set()
        for member in self.members:
            if not isinstance(member, CodingModeSnapshotMemberV1):
                raise CodingModeSnapshotError("member_type_invalid")
            _, folded = _path(member.relative_path)
            if folded in seen:
                raise CodingModeSnapshotError("member_collision")
            seen.add(folded)
        object.__setattr__(self, "snapshot", state)
        object.__setattr__(self, "reason", reason)
        if state in {CodingModeSnapshotState.EMPTY, CodingModeSnapshotState.BLOCKED} and self.members:
            raise CodingModeSnapshotError("non_snapshot_members_exposed")
        if state is CodingModeSnapshotState.SNAPSHOT and not self.members:
            raise CodingModeSnapshotError("snapshot_without_members")

    @property
    def state(self) -> CodingModeSnapshotState:
        return self.snapshot

    @property
    def decision(self) -> CodingModeSnapshotState:
        return self.snapshot

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(member.relative_path for member in self.members)

    @property
    def files(self) -> tuple[CodingModeSnapshotMemberV1, ...]:
        return self.members

    @property
    def digests(self) -> dict[str, str]:
        return {member.relative_path: member.sha256 for member in self.members}

    @property
    def closed_reason(self) -> CodingModeSnapshotReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_MODE_SNAPSHOT_SCHEMA,
            "snapshot_id": self.snapshot_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "snapshot": self.snapshot.value,
            "members": [member.to_mapping() for member in self.members],
            "reason": self.reason.value,
        }


def _state(value: object) -> CodingModeSnapshotState:
    try:
        return value if isinstance(value, CodingModeSnapshotState) else CodingModeSnapshotState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingModeSnapshotError("snapshot_closed") from exc


def _reason(value: object) -> CodingModeSnapshotReason:
    try:
        return value if isinstance(value, CodingModeSnapshotReason) else CodingModeSnapshotReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingModeSnapshotError("reason_closed") from exc


def _members(value: object) -> tuple[CodingModeSnapshotMemberV1, ...]:
    if isinstance(value, Mapping):
        raw: Sequence[object] = tuple((key, item) for key, item in value.items())
    elif isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise CodingModeSnapshotError("members_invalid")
    else:
        raw = cast(Sequence[object], value)
    if len(raw) > MAX_SNAPSHOT_MEMBERS:
        raise CodingModeSnapshotError("member_limit")
    result: list[CodingModeSnapshotMemberV1] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, CodingModeSnapshotMemberV1):
            member = item
        elif isinstance(item, Mapping):
            allowed = {"relative_path", "path", "name", "filename", "sha256", "digest", "digest_sha256"}
            if set(item) - allowed:
                raise CodingModeSnapshotError("member_unknown_fields")
            name = item.get("relative_path", item.get("path", item.get("name", item.get("filename"))))
            digest = item.get("sha256", item.get("digest", item.get("digest_sha256")))
            member = CodingModeSnapshotMemberV1(cast(str, name), cast(str, digest))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            member = CodingModeSnapshotMemberV1(cast(str, item[0]), cast(str, item[1]))
        else:
            raise CodingModeSnapshotError("member_type_invalid")
        _, folded = _path(member.relative_path)
        if folded in seen:
            raise CodingModeSnapshotError("casefold_collision")
        seen.add(folded)
        result.append(member)
    return tuple(sorted(result, key=lambda member: (member.relative_path.casefold(), member.relative_path)))


def _result(
    snapshot_id: str,
    turn: str,
    state: CodingModeSnapshotState,
    reason: CodingModeSnapshotReason,
    members: tuple[CodingModeSnapshotMemberV1, ...] = (),
) -> CodingModeSnapshotV1:
    if state is not CodingModeSnapshotState.SNAPSHOT:
        members = ()
    return CodingModeSnapshotV1(snapshot_id, turn, state, members, reason)


def build_coding_mode_snapshot(
    snapshot_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    members: object = None,
    *,
    files: object = None,
    names: object = None,
) -> CodingModeSnapshotV1:
    """Bind relative names to supplied SHA-256 facts without reading bytes."""

    if isinstance(snapshot_id, Mapping):
        raw = snapshot_id
        allowed = {
            "schema",
            "snapshot_id",
            "authenticated_turn_id",
            "snapshot",
            "state",
            "members",
            "files",
            "names",
            "reason",
        }
        if set(raw) - allowed:
            raise CodingModeSnapshotError("snapshot_mapping_unknown_fields")
        if {"snapshot", "state", "reason"}.intersection(raw):
            required = {"schema", "snapshot_id", "authenticated_turn_id", "snapshot", "members", "reason"}
            if set(raw) != required or raw.get("schema") != CODING_MODE_SNAPSHOT_SCHEMA:
                raise CodingModeSnapshotError("snapshot_mapping_serialized_invalid")
            return CodingModeSnapshotV1(
                cast(str, raw.get("snapshot_id")),
                cast(str, raw.get("authenticated_turn_id")),
                cast(CodingModeSnapshotState, raw.get("snapshot", raw.get("state"))),
                tuple(_members(raw.get("members", ()))),
                cast(CodingModeSnapshotReason, raw.get("reason")),
            )
        if any(value is not None for value in (members, files, names)):
            raise CodingModeSnapshotError("snapshot_mapping_and_facts_mixed")
        snapshot_id = cast(str, raw.get("snapshot_id"))
        authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
        members = raw.get("members", raw.get("files", raw.get("names")))
    if files is not None:
        if members is not None:
            raise CodingModeSnapshotError("snapshot_duplicate_members")
        members = files
    if names is not None:
        if members is not None:
            raise CodingModeSnapshotError("snapshot_duplicate_members")
        members = names
    snapshot_key = _identifier(snapshot_id, "snapshot_id", MAX_SNAPSHOT_ID_CHARS)
    turn_key = _identifier(authenticated_turn_id, "authenticated_turn_id", MAX_AUTHENTICATED_TURN_ID_CHARS)
    if members is None:
        return _result(snapshot_key, turn_key, CodingModeSnapshotState.EMPTY, CodingModeSnapshotReason.NO_MEMBERS)
    try:
        values = _members(members)
    except CodingModeSnapshotError as exc:
        reason = CodingModeSnapshotReason.INVALID_FACTS
        message = str(exc)
        if "travers" in message:
            reason = CodingModeSnapshotReason.PATH_TRAVERSAL
        elif "absolute" in message:
            reason = CodingModeSnapshotReason.ABSOLUTE_PATH
        elif "secret" in message:
            reason = CodingModeSnapshotReason.SECRET_NAME
        elif "collision" in message:
            reason = CodingModeSnapshotReason.CASEFOLD_COLLISION
        elif "digest" in message:
            reason = CodingModeSnapshotReason.INVALID_DIGEST
        elif "limit" in message:
            reason = CodingModeSnapshotReason.MEMBER_LIMIT
        return _result(snapshot_key, turn_key, CodingModeSnapshotState.BLOCKED, reason)
    if not values:
        return _result(snapshot_key, turn_key, CodingModeSnapshotState.EMPTY, CodingModeSnapshotReason.NO_MEMBERS)
    return _result(snapshot_key, turn_key, CodingModeSnapshotState.SNAPSHOT, CodingModeSnapshotReason.SNAPSHOT_BOUND, values)


build_mode_snapshot = build_coding_mode_snapshot


__all__ = [
    "CODING_MODE_SNAPSHOT_SCHEMA",
    "MAX_AUTHENTICATED_TURN_ID_CHARS",
    "MAX_SNAPSHOT_ID_CHARS",
    "MAX_SNAPSHOT_MEMBERS",
    "MAX_SNAPSHOT_PATH_CHARS",
    "CodingModeSnapshotError",
    "CodingModeSnapshotMemberV1",
    "CodingModeSnapshotReason",
    "CodingModeSnapshotState",
    "CodingModeSnapshotV1",
    "build_coding_mode_snapshot",
    "build_mode_snapshot",
]
