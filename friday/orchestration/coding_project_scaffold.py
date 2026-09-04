"""Pure planned file scaffold for prompt-to-small-project.

The scaffold lists relative files that would be created.  It does not write
bytes, open a path, or call a coding worker.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

CODING_PROJECT_SCAFFOLD_SCHEMA = "friday.coding-project-scaffold.v1"
MAX_SCAFFOLD_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_SCAFFOLD_FILES = 32
MAX_FILE_PATH_CHARS = 256

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SECRET_NAME_RE = re.compile(r"(?i)(?:^|/)(?:\.env|id_rsa|id_ed25519|credentials|secrets?)(?:$|\.)")


class CodingProjectScaffoldError(ValueError):
    """A scaffold identity, file fact, or result is malformed."""


class CodingProjectScaffoldState(StrEnum):
    EMPTY = "empty"
    SCAFFOLDED = "scaffolded"
    BLOCKED = "blocked"


class CodingProjectScaffoldReason(StrEnum):
    NO_FILES = "no_files"
    ALL_FILES_PLANNED = "all_files_planned"
    FILE_LIMIT = "file_limit"
    UNSAFE_PATH = "unsafe_path"
    SECRET_NAME = "secret_name"
    CASEFOLD_COLLISION = "casefold_collision"
    INVALID_FACTS = "invalid_facts"


class CodingScaffoldFileKind(StrEnum):
    FILE = "file"


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise CodingProjectScaffoldError(f"{field}_{detail}")


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _state(value: object) -> CodingProjectScaffoldState:
    try:
        return CodingProjectScaffoldState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingProjectScaffoldError("scaffold_closed") from exc


def _reason(value: object) -> CodingProjectScaffoldReason:
    try:
        return CodingProjectScaffoldReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingProjectScaffoldError("reason_closed") from exc


def _path(value: object) -> str:
    if type(value) is not str or not value or len(value) > MAX_FILE_PATH_CHARS:
        _fail("path", "path")
    path = cast(str, value)
    if path != path.strip() or any(unicodedata.category(character).startswith("C") for character in path):
        _fail("path", "path")
    if path.startswith(("/", "\\")) or _DRIVE_RE.match(path) is not None:
        _fail("path", "absolute")
    parts = tuple(part for part in re.split(r"[/\\]", path) if part)
    if not parts or any(part in {".", ".."} for part in parts):
        _fail("path", "traversal")
    joined = "/".join(parts)
    if _SECRET_NAME_RE.search(joined) is not None:
        _fail("path", "secret")
    return joined


@dataclass(frozen=True, slots=True)
class CodingScaffoldFileV1:
    path: str
    kind: CodingScaffoldFileKind = CodingScaffoldFileKind.FILE

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _path(self.path))
        try:
            object.__setattr__(self, "kind", CodingScaffoldFileKind(cast(str, self.kind)))
        except (TypeError, ValueError) as exc:
            raise CodingProjectScaffoldError("kind_closed") from exc
        if self.kind is not CodingScaffoldFileKind.FILE:
            _fail("kind", "unsupported")

    def to_mapping(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind.value}


@dataclass(frozen=True, slots=True)
class CodingProjectScaffoldV1:
    scaffold_id: str
    authenticated_turn_id: str
    scaffold: CodingProjectScaffoldState
    files: tuple[CodingScaffoldFileV1, ...]
    reason: CodingProjectScaffoldReason

    def __post_init__(self) -> None:
        _identifier(self.scaffold_id, field="scaffold_id", maximum=MAX_SCAFFOLD_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        state = _state(self.scaffold)
        reason = _reason(self.reason)
        object.__setattr__(self, "scaffold", state)
        object.__setattr__(self, "reason", reason)
        if not isinstance(self.files, tuple) or len(self.files) > MAX_SCAFFOLD_FILES:
            _fail("files", "count")
        if state is CodingProjectScaffoldState.SCAFFOLDED:
            if not self.files:
                _fail("files", "missing")
            seen: set[str] = set()
            for item in self.files:
                if not isinstance(item, CodingScaffoldFileV1):
                    _fail("files", "type")
                item.__post_init__()
                folded = unicodedata.normalize("NFC", item.path).casefold()
                if folded in seen:
                    _fail("files", "collision")
                seen.add(folded)
        elif self.files:
            _fail("blocked_or_empty_scaffold", "exposed")

    @property
    def state(self) -> CodingProjectScaffoldState:
        return self.scaffold

    @property
    def closed_reason(self) -> CodingProjectScaffoldReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_PROJECT_SCAFFOLD_SCHEMA,
            "scaffold_id": self.scaffold_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "scaffold": self.scaffold.value,
            "files": [item.to_mapping() for item in self.files],
            "reason": self.reason.value,
        }


def _file(value: object) -> CodingScaffoldFileV1:
    if isinstance(value, CodingScaffoldFileV1):
        value.__post_init__()
        return value
    if type(value) is str:
        return CodingScaffoldFileV1(value)
    if not isinstance(value, Mapping):
        _fail("file", "type")
    allowed = {"path", "relative_path", "kind"}
    if set(value) - allowed:
        _fail("file", "unknown_fields")
    return CodingScaffoldFileV1(
        path=cast(str, value.get("path", value.get("relative_path"))),
        kind=cast(CodingScaffoldFileKind, value.get("kind", CodingScaffoldFileKind.FILE)),
    )


def _result(
    scaffold_id: str,
    authenticated_turn_id: str,
    state: CodingProjectScaffoldState,
    reason: CodingProjectScaffoldReason,
    files: tuple[CodingScaffoldFileV1, ...] = (),
) -> CodingProjectScaffoldV1:
    if state is not CodingProjectScaffoldState.SCAFFOLDED:
        files = ()
    return CodingProjectScaffoldV1(
        scaffold_id=scaffold_id,
        authenticated_turn_id=authenticated_turn_id,
        scaffold=state,
        files=files,
        reason=reason,
    )


def build_coding_project_scaffold(
    scaffold_id: str,
    authenticated_turn_id: str,
    files: Sequence[object] | None = None,
) -> CodingProjectScaffoldV1:
    """Admit a relative file list; secrets, traversal and collisions fail closed."""

    identity = _identifier(scaffold_id, field="scaffold_id", maximum=MAX_SCAFFOLD_ID_CHARS)
    turn = _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    if files is None:
        return _result(
            identity,
            turn,
            CodingProjectScaffoldState.EMPTY,
            CodingProjectScaffoldReason.NO_FILES,
        )
    if isinstance(files, (str, bytes, bytearray)) or not isinstance(files, Sequence):
        return _result(
            identity,
            turn,
            CodingProjectScaffoldState.BLOCKED,
            CodingProjectScaffoldReason.INVALID_FACTS,
        )
    if len(files) > MAX_SCAFFOLD_FILES:
        return _result(
            identity,
            turn,
            CodingProjectScaffoldState.BLOCKED,
            CodingProjectScaffoldReason.FILE_LIMIT,
        )
    if not files:
        return _result(
            identity,
            turn,
            CodingProjectScaffoldState.EMPTY,
            CodingProjectScaffoldReason.NO_FILES,
        )
    planned: list[CodingScaffoldFileV1] = []
    seen: set[str] = set()
    try:
        for item in files:
            parsed = _file(item)
            folded = unicodedata.normalize("NFC", parsed.path).casefold()
            if folded in seen:
                _fail("files", "collision")
            seen.add(folded)
            planned.append(parsed)
    except CodingProjectScaffoldError as exc:
        code = str(exc)
        if code == "path_secret":
            reason = CodingProjectScaffoldReason.SECRET_NAME
        elif code == "files_collision":
            reason = CodingProjectScaffoldReason.CASEFOLD_COLLISION
        elif code.endswith(("_absolute", "_traversal", "_path")):
            reason = CodingProjectScaffoldReason.UNSAFE_PATH
        else:
            reason = CodingProjectScaffoldReason.INVALID_FACTS
        return _result(identity, turn, CodingProjectScaffoldState.BLOCKED, reason)
    return _result(
        identity,
        turn,
        CodingProjectScaffoldState.SCAFFOLDED,
        CodingProjectScaffoldReason.ALL_FILES_PLANNED,
        tuple(planned),
    )


scaffold_coding_project = build_coding_project_scaffold

__all__ = [
    "CODING_PROJECT_SCAFFOLD_SCHEMA",
    "CodingProjectScaffoldError",
    "CodingProjectScaffoldReason",
    "CodingProjectScaffoldState",
    "CodingProjectScaffoldV1",
    "CodingScaffoldFileKind",
    "CodingScaffoldFileV1",
    "build_coding_project_scaffold",
    "scaffold_coding_project",
]
