"""Pure manifest admission for one final Coding Mode source archive.

The manifest consumes names and lowercase SHA-256 facts supplied by an
upstream observer.  It never opens a path, hashes bytes, or packs an archive.
Unsafe names and secrets fail closed, and blocked results retain no member
names or digests.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

CODING_RESULT_ARCHIVE_MANIFEST_SCHEMA = "friday.coding-result-archive-manifest.v1"
MAX_MANIFEST_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_MANIFEST_MEMBER_COUNT = 32
MAX_MANIFEST_PATH_CHARS = 4_096

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class CodingResultArchiveManifestError(ValueError):
    """A manifest identity, member name, or digest fact is malformed."""


class CodingResultArchiveManifestState(StrEnum):
    EMPTY = "empty"
    LISTED = "listed"
    BLOCKED = "blocked"


class CodingResultArchiveManifestReason(StrEnum):
    NO_FILES = "no_files"
    FILES_LISTED = "files_listed"
    UNSAFE_PATH = "unsafe_path"
    SECRET_NAME = "secret_name"
    CASEFOLD_COLLISION = "casefold_collision"
    INVALID_DIGEST = "invalid_digest"
    MISSING_DIGEST = "missing_digest"
    FILE_LIMIT = "file_limit"
    INVALID_FACTS = "invalid_facts"

    NO_MEMBER_FILES = NO_FILES
    MANIFEST_LISTED = FILES_LISTED
    CASE_FOLD_COLLISION = CASEFOLD_COLLISION


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise CodingResultArchiveManifestError(f"{field}_{detail}")


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _state(value: object) -> CodingResultArchiveManifestState:
    try:
        return CodingResultArchiveManifestState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingResultArchiveManifestError("manifest_closed") from exc


def _reason(value: object) -> CodingResultArchiveManifestReason:
    try:
        return CodingResultArchiveManifestReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingResultArchiveManifestError("reason_closed") from exc


def _sha256(value: object, *, field: str = "sha256") -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _fail(field, "hex")
    return cast(str, value)


def _path(value: object, *, field: str = "relative_path") -> tuple[str, str]:
    if type(value) is not str or not value or len(value) > MAX_MANIFEST_PATH_CHARS:
        _fail(field, "path")
    raw = cast(str, value)
    if raw != raw.strip() or any(unicodedata.category(char).startswith("C") for char in raw):
        _fail(field, "path")
    if raw.startswith(("/", "\\")) or _DRIVE_RE.match(raw) is not None:
        _fail(field, "absolute")
    parts = tuple(part for part in re.split(r"[/\\]", raw) if part)
    if not parts or any(part in {".", ".."} for part in parts):
        _fail(field, "traversal")
    canonical = "/".join(parts)
    folded = unicodedata.normalize("NFC", canonical).casefold()
    return canonical, folded


def _secret(path: str) -> bool:
    for part in path.split("/"):
        folded = part.casefold()
        if (
            folded == ".env"
            or folded.startswith(".env.")
            or folded in {"id_rsa", "id_ed25519", "credentials", "credential"}
            or folded.startswith("secret")
        ):
            return True
    return False


@dataclass(frozen=True, slots=True)
class CodingResultArchiveManifestMemberV1:
    """One already-supplied source name and its lowercase SHA-256 fact."""

    relative_path: str
    sha256: str

    def __post_init__(self) -> None:
        canonical, _ = _path(self.relative_path)
        if _secret(canonical):
            _fail("relative_path", "secret")
        object.__setattr__(self, "relative_path", canonical)
        _sha256(self.sha256)

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


def _members(value: object) -> tuple[CodingResultArchiveManifestMemberV1, ...]:
    if isinstance(value, Mapping):
        if not value:
            return ()
        raw_items: Sequence[object] = tuple((key, item) for key, item in value.items())
    elif isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail("members", "sequence")
    else:
        raw_items = cast(Sequence[object], value)
    if len(raw_items) > MAX_MANIFEST_MEMBER_COUNT:
        _fail("members", "count")
    result: list[CodingResultArchiveManifestMemberV1] = []
    seen: set[str] = set()
    for item in raw_items:
        if isinstance(item, CodingResultArchiveManifestMemberV1):
            member = item
        elif isinstance(item, Mapping):
            allowed = {
                "relative_path",
                "path",
                "filename",
                "sha256",
                "digest",
                "digest_sha256",
                "file_sha256",
            }
            if set(item) - allowed:
                _fail("member", "unknown_fields")
            name = item.get("relative_path", item.get("path", item.get("filename")))
            digest = item.get(
                "sha256",
                item.get("digest_sha256", item.get("file_sha256", item.get("digest"))),
            )
            member = CodingResultArchiveManifestMemberV1(cast(str, name), cast(str, digest))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            member = CodingResultArchiveManifestMemberV1(cast(str, item[0]), cast(str, item[1]))
        else:
            _fail("member", "type")
        _, folded = _path(member.relative_path)
        if folded in seen:
            _fail("members", "collision")
        seen.add(folded)
        result.append(member)
    return tuple(
        sorted(
            result,
            key=lambda member: (
                unicodedata.normalize("NFC", member.relative_path).casefold(),
                member.relative_path,
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class CodingResultArchiveManifestV1:
    """Immutable source-name and digest manifest for one archive plan."""

    manifest_id: str
    authenticated_turn_id: str
    manifest: CodingResultArchiveManifestState
    members: tuple[CodingResultArchiveManifestMemberV1, ...]
    reason: CodingResultArchiveManifestReason

    def __post_init__(self) -> None:
        _identifier(self.manifest_id, field="manifest_id", maximum=MAX_MANIFEST_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        state = _state(self.manifest)
        reason = _reason(self.reason)
        object.__setattr__(self, "manifest", state)
        object.__setattr__(self, "reason", reason)
        members = _members(self.members)
        object.__setattr__(self, "members", members)
        if state is CodingResultArchiveManifestState.LISTED and not members:
            _fail("listed_manifest", "missing_members")
        if (
            state in {CodingResultArchiveManifestState.EMPTY, CodingResultArchiveManifestState.BLOCKED}
            and members
        ):
            _fail("nonlisted_manifest", "exposed")

    @property
    def state(self) -> CodingResultArchiveManifestState:
        return self.manifest

    @property
    def entries(self) -> tuple[CodingResultArchiveManifestMemberV1, ...]:
        return self.members

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(member.relative_path for member in self.members)

    @property
    def digests(self) -> dict[str, str]:
        return {member.relative_path: member.sha256 for member in self.members}

    @property
    def closed_reason(self) -> CodingResultArchiveManifestReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_RESULT_ARCHIVE_MANIFEST_SCHEMA,
            "manifest_id": self.manifest_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "manifest": self.manifest.value,
            "members": [member.to_mapping() for member in self.members],
            "reason": self.reason.value,
        }


ManifestState = CodingResultArchiveManifestState
ManifestReason = CodingResultArchiveManifestReason
CodingResultArchiveManifest = CodingResultArchiveManifestV1
CodingResultArchiveManifestEntryV1 = CodingResultArchiveManifestMemberV1
CodingResultArchiveManifestEntry = CodingResultArchiveManifestMemberV1


def _result(
    manifest_id: str,
    turn: str,
    state: CodingResultArchiveManifestState,
    reason: CodingResultArchiveManifestReason,
    members: tuple[CodingResultArchiveManifestMemberV1, ...] = (),
) -> CodingResultArchiveManifestV1:
    if state in {CodingResultArchiveManifestState.EMPTY, CodingResultArchiveManifestState.BLOCKED}:
        members = ()
    return CodingResultArchiveManifestV1(manifest_id, turn, state, members, reason)


def _known_mapping_keys(raw: Mapping[str, Any]) -> None:
    known = {
        "schema",
        "manifest_id",
        "authenticated_turn_id",
        "members",
        "entries",
        "files",
        "digests",
        "manifest",
        "state",
        "reason",
    }
    if set(raw) - known:
        _fail("manifest", "unknown_fields")


def _input_members(raw: Mapping[str, Any]) -> object:
    if "members" in raw:
        return raw["members"]
    if "entries" in raw:
        return raw["entries"]
    if "files" in raw:
        files = raw["files"]
        if "digests" in raw:
            if not isinstance(files, Sequence) or isinstance(files, (str, bytes, bytearray)):
                _fail("files", "sequence")
            digest_map = raw["digests"]
            if not isinstance(digest_map, Mapping):
                _fail("digests", "mapping")
            return tuple({"relative_path": name, "sha256": digest_map.get(name)} for name in files)
        return files
    if "digests" in raw:
        return raw["digests"]
    return None


def build_coding_result_archive_manifest(
    manifest_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    members: object = None,
    *,
    files: object = None,
    entries: object = None,
    digests: object = None,
) -> CodingResultArchiveManifestV1:
    """Admit supplied relative names and SHA-256 facts without reading bytes."""

    if isinstance(manifest_id, Mapping):
        raw = manifest_id
        _known_mapping_keys(raw)
        if raw.get("schema", CODING_RESULT_ARCHIVE_MANIFEST_SCHEMA) != CODING_RESULT_ARCHIVE_MANIFEST_SCHEMA:
            _fail("schema")
        output_keys = {"manifest", "state", "reason"}
        input_keys = {"entries", "files", "digests"}
        if output_keys.intersection(raw) and input_keys.intersection(raw):
            _fail("manifest", "duplicate_representations")
        if output_keys.intersection(raw):
            return CodingResultArchiveManifestV1(
                manifest_id=cast(str, raw.get("manifest_id")),
                authenticated_turn_id=cast(str, raw.get("authenticated_turn_id")),
                manifest=cast(CodingResultArchiveManifestState, raw.get("manifest", raw.get("state"))),
                members=cast(tuple[CodingResultArchiveManifestMemberV1, ...], raw.get("members", ())),
                reason=cast(CodingResultArchiveManifestReason, raw.get("reason")),
            )
        manifest_id = cast(str, raw.get("manifest_id"))
        authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
        members = _input_members(raw)

    if members is not None and (files is not None or entries is not None or digests is not None):
        _fail("manifest", "duplicate_arguments")
    if members is None:
        if entries is not None:
            members = entries
        elif files is not None:
            if digests is not None:
                if not isinstance(files, Sequence) or isinstance(files, (str, bytes, bytearray)):
                    _fail("files", "sequence")
                if not isinstance(digests, Mapping):
                    _fail("digests", "mapping")
                members = tuple({"relative_path": name, "sha256": digests.get(name)} for name in files)
            else:
                members = files
        elif digests is not None:
            members = digests

    manifest_key = _identifier(manifest_id, field="manifest_id", maximum=MAX_MANIFEST_ID_CHARS)
    turn_key = _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    if members is None:
        return _result(
            manifest_key,
            turn_key,
            CodingResultArchiveManifestState.EMPTY,
            CodingResultArchiveManifestReason.NO_FILES,
        )
    try:
        listed = _members(members)
    except CodingResultArchiveManifestError as exc:
        detail = str(exc)
        if "secret" in detail:
            reason = CodingResultArchiveManifestReason.SECRET_NAME
        elif "collision" in detail:
            reason = CodingResultArchiveManifestReason.CASEFOLD_COLLISION
        elif "absolute" in detail or "traversal" in detail or "path" in detail:
            reason = CodingResultArchiveManifestReason.UNSAFE_PATH
        elif "hex" in detail:
            reason = CodingResultArchiveManifestReason.INVALID_DIGEST
        elif "count" in detail:
            reason = CodingResultArchiveManifestReason.FILE_LIMIT
        else:
            reason = CodingResultArchiveManifestReason.INVALID_FACTS
        return _result(manifest_key, turn_key, CodingResultArchiveManifestState.BLOCKED, reason)
    if not listed:
        return _result(
            manifest_key,
            turn_key,
            CodingResultArchiveManifestState.EMPTY,
            CodingResultArchiveManifestReason.NO_FILES,
        )
    return _result(
        manifest_key,
        turn_key,
        CodingResultArchiveManifestState.LISTED,
        CodingResultArchiveManifestReason.FILES_LISTED,
        listed,
    )


def validate_coding_result_archive_manifest(value: object) -> bool:
    """Return whether a manifest object or its serialized mapping is closed."""

    try:
        if isinstance(value, CodingResultArchiveManifestV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping):
            return False
        _known_mapping_keys(value)
        required = {"schema", "manifest_id", "authenticated_turn_id", "manifest", "members", "reason"}
        if set(value) != required or value.get("schema") != CODING_RESULT_ARCHIVE_MANIFEST_SCHEMA:
            return False
        CodingResultArchiveManifestV1(
            manifest_id=cast(str, value.get("manifest_id")),
            authenticated_turn_id=cast(str, value.get("authenticated_turn_id")),
            manifest=cast(CodingResultArchiveManifestState, value.get("manifest")),
            members=cast(tuple[CodingResultArchiveManifestMemberV1, ...], value.get("members")),
            reason=cast(CodingResultArchiveManifestReason, value.get("reason")),
        )
        return True
    except (TypeError, ValueError):
        return False


build_archive_manifest = build_coding_result_archive_manifest
plan_coding_result_archive_manifest = build_coding_result_archive_manifest
validate_archive_manifest = validate_coding_result_archive_manifest


__all__ = [
    "CODING_RESULT_ARCHIVE_MANIFEST_SCHEMA",
    "MAX_AUTHENTICATED_TURN_ID_CHARS",
    "MAX_MANIFEST_ID_CHARS",
    "MAX_MANIFEST_MEMBER_COUNT",
    "MAX_MANIFEST_PATH_CHARS",
    "CodingResultArchiveManifest",
    "CodingResultArchiveManifestEntry",
    "CodingResultArchiveManifestEntryV1",
    "CodingResultArchiveManifestError",
    "CodingResultArchiveManifestMemberV1",
    "CodingResultArchiveManifestReason",
    "CodingResultArchiveManifestState",
    "CodingResultArchiveManifestV1",
    "ManifestReason",
    "ManifestState",
    "build_archive_manifest",
    "build_coding_result_archive_manifest",
    "plan_coding_result_archive_manifest",
    "validate_archive_manifest",
    "validate_coding_result_archive_manifest",
]
