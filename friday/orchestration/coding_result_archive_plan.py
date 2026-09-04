"""Pure plan for one final Coding Mode source archive.

The planner consumes an already-mapped source tree or already-inventoried
relative paths.  It does not open a path, pack bytes, spawn a worker, or wire
``/coding``.  Directories, links, secrets, internal receipts and unsafe paths
fail closed.  Zero user files is text; one ordinary file is sent directly;
two or more files become one archive.  A one-file ZIP is forbidden.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from friday.orchestration.coding_source_member import (
    CodingSourceFileKind,
    CodingSourceLinkKind,
    CodingSourceMemberV1,
)
from friday.orchestration.coding_source_tree import (
    CodingSourceTreeState,
    CodingSourceTreeV1,
    build_coding_source_tree,
)
from friday.orchestration.engineer_result_carrier import (
    EngineerResultCarrierKind,
    EngineerResultPolicyError,
)
from friday.orchestration.operation_result_carrier import (
    MAX_OPERATION_RESULT_FILES,
    select_operation_result_carrier,
    select_user_result_files,
)

CODING_RESULT_ARCHIVE_PLAN_SCHEMA = "friday.coding-result-archive-plan.v1"
CODING_RESULT_ARCHIVE_FILENAME = "friday-source.zip"
MAX_PLAN_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_ARCHIVE_FILES = MAX_OPERATION_RESULT_FILES

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SECRET_NAME_RE = re.compile(r"(?i)(?:^|/)(?:\.env|id_rsa|id_ed25519|credentials|secrets?)(?:$|\.)")


class CodingResultArchivePlanError(ValueError):
    """A result-archive identity, member fact, or plan is malformed."""


class CodingResultArchivePlanState(StrEnum):
    EMPTY = "empty"
    FILE = "file"
    ARCHIVE = "archive"
    BLOCKED = "blocked"


class CodingResultArchivePlanReason(StrEnum):
    NO_USER_FILES = "no_user_files"
    ONE_USER_FILE = "one_user_file"
    MULTIPLE_USER_FILES = "multiple_user_files"
    TREE_BLOCKED = "tree_blocked"
    TREE_INVALID = "tree_invalid"
    SECRET_NAME = "secret_name"
    UNSAFE_PATH = "unsafe_path"
    CASEFOLD_COLLISION = "casefold_collision"
    FILE_LIMIT = "file_limit"
    IDENTITY_MISMATCH = "identity_mismatch"
    INVALID_FACTS = "invalid_facts"


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise CodingResultArchivePlanError(f"{field}_{detail}")


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _state(value: object) -> CodingResultArchivePlanState:
    try:
        return CodingResultArchivePlanState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingResultArchivePlanError("plan_closed") from exc


def _reason(value: object) -> CodingResultArchivePlanReason:
    try:
        return CodingResultArchivePlanReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingResultArchivePlanError("reason_closed") from exc


def _paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        _fail("files", "type")
    if len(value) > MAX_ARCHIVE_FILES:
        _fail("files", "count")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if type(item) is not str or not item:
            _fail("files", "path")
        folded = unicodedata.normalize("NFC", item).casefold()
        if folded in seen:
            _fail("files", "collision")
        seen.add(folded)
        result.append(item)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class CodingResultArchivePlanV1:
    """Immutable TEXT/FILE/ARCHIVE choice for already-inventoried source files."""

    plan_id: str
    authenticated_turn_id: str
    plan: CodingResultArchivePlanState
    files: tuple[str, ...]
    reason: CodingResultArchivePlanReason

    def __post_init__(self) -> None:
        _identifier(self.plan_id, field="plan_id", maximum=MAX_PLAN_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        state = _state(self.plan)
        reason = _reason(self.reason)
        object.__setattr__(self, "plan", state)
        object.__setattr__(self, "reason", reason)
        paths = _paths(self.files)
        object.__setattr__(self, "files", paths)
        if state is CodingResultArchivePlanState.BLOCKED and paths:
            _fail("blocked_plan", "exposed")
        if state is CodingResultArchivePlanState.EMPTY and paths:
            _fail("empty_plan", "exposed")
        if state is CodingResultArchivePlanState.FILE and len(paths) != 1:
            _fail("file_plan", "count")
        if state is CodingResultArchivePlanState.ARCHIVE and len(paths) < 2:
            _fail("archive_plan", "count")

    @property
    def state(self) -> CodingResultArchivePlanState:
        return self.plan

    @property
    def carrier(self) -> EngineerResultCarrierKind:
        if self.plan is CodingResultArchivePlanState.FILE:
            return EngineerResultCarrierKind.FILE
        if self.plan is CodingResultArchivePlanState.ARCHIVE:
            return EngineerResultCarrierKind.ARCHIVE
        return EngineerResultCarrierKind.TEXT

    @property
    def closed_reason(self) -> CodingResultArchivePlanReason:
        return self.reason

    @property
    def archive_filename(self) -> str | None:
        if self.plan is CodingResultArchivePlanState.ARCHIVE:
            return CODING_RESULT_ARCHIVE_FILENAME
        return None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_RESULT_ARCHIVE_PLAN_SCHEMA,
            "plan_id": self.plan_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "plan": self.plan.value,
            "carrier": self.carrier.value,
            "files": list(self.files),
            "reason": self.reason.value,
        }


def _result(
    plan_id: str,
    authenticated_turn_id: str,
    state: CodingResultArchivePlanState,
    reason: CodingResultArchivePlanReason,
    files: tuple[str, ...] = (),
) -> CodingResultArchivePlanV1:
    if state in {CodingResultArchivePlanState.EMPTY, CodingResultArchivePlanState.BLOCKED}:
        files = ()
    return CodingResultArchivePlanV1(
        plan_id=plan_id,
        authenticated_turn_id=authenticated_turn_id,
        plan=state,
        files=files,
        reason=reason,
    )


def _tree(value: object) -> CodingSourceTreeV1 | None:
    try:
        if isinstance(value, CodingSourceTreeV1):
            value.__post_init__()
            return value
        if isinstance(value, Mapping):
            return build_coding_source_tree(
                cast(str, value.get("tree_id")),
                cast(str, value.get("authenticated_turn_id")),
                value.get("members", value.get("source_members")),
            )
    except (TypeError, ValueError):
        return None
    return None


def _paths_from_tree(tree: CodingSourceTreeV1) -> tuple[str, ...] | CodingResultArchivePlanReason:
    if tree.tree is CodingSourceTreeState.BLOCKED:
        return CodingResultArchivePlanReason.TREE_BLOCKED
    if tree.tree is CodingSourceTreeState.EMPTY:
        return ()
    paths: list[str] = []
    for member in tree.members:
        if not isinstance(member, CodingSourceMemberV1):
            return CodingResultArchivePlanReason.TREE_INVALID
        if member.link_kind is not CodingSourceLinkKind.NONE:
            return CodingResultArchivePlanReason.TREE_BLOCKED
        if member.file_kind is CodingSourceFileKind.DIRECTORY:
            continue
        paths.append(member.relative_path)
    return tuple(paths)


def _paths_from_files(files: object) -> tuple[str, ...] | CodingResultArchivePlanReason:
    if isinstance(files, (str, bytes, bytearray)) or not isinstance(files, Sequence):
        return CodingResultArchivePlanReason.INVALID_FACTS
    paths: list[str] = []
    for item in files:
        if type(item) is str:
            paths.append(item)
            continue
        if isinstance(item, Mapping):
            path = item.get("relative_path", item.get("path", item.get("filename")))
            if type(path) is str:
                paths.append(path)
                continue
        return CodingResultArchivePlanReason.INVALID_FACTS
    return tuple(paths)


def _blocked_from_policy(exc: EngineerResultPolicyError) -> CodingResultArchivePlanReason:
    code = exc.code
    if code == "result_file_duplicate":
        return CodingResultArchivePlanReason.CASEFOLD_COLLISION
    if code in {"result_path_invalid", "result_file_invalid"}:
        return CodingResultArchivePlanReason.UNSAFE_PATH
    if code == "result_file_count_limit":
        return CodingResultArchivePlanReason.FILE_LIMIT
    return CodingResultArchivePlanReason.INVALID_FACTS


def build_coding_result_archive_plan(
    plan_id: str,
    authenticated_turn_id: str,
    tree: object = None,
    files: object = None,
    *,
    archive_requested: bool = False,
) -> CodingResultArchivePlanV1:
    """Admit one TEXT/FILE/ARCHIVE carrier from already-inventoried source files."""

    identity = _identifier(plan_id, field="plan_id", maximum=MAX_PLAN_ID_CHARS)
    turn = _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    if not isinstance(archive_requested, bool):
        return _result(
            identity, turn, CodingResultArchivePlanState.BLOCKED, CodingResultArchivePlanReason.INVALID_FACTS
        )
    if tree is not None and files is not None:
        return _result(
            identity, turn, CodingResultArchivePlanState.BLOCKED, CodingResultArchivePlanReason.INVALID_FACTS
        )

    raw_paths: tuple[str, ...] = ()
    if tree is not None:
        tree_value = _tree(tree)
        if tree_value is None:
            return _result(
                identity,
                turn,
                CodingResultArchivePlanState.BLOCKED,
                CodingResultArchivePlanReason.TREE_INVALID,
            )
        if tree_value.authenticated_turn_id != turn:
            return _result(
                identity,
                turn,
                CodingResultArchivePlanState.BLOCKED,
                CodingResultArchivePlanReason.IDENTITY_MISMATCH,
            )
        extracted = _paths_from_tree(tree_value)
        if isinstance(extracted, CodingResultArchivePlanReason):
            return _result(identity, turn, CodingResultArchivePlanState.BLOCKED, extracted)
        raw_paths = extracted
    elif files is not None:
        extracted = _paths_from_files(files)
        if isinstance(extracted, CodingResultArchivePlanReason):
            return _result(identity, turn, CodingResultArchivePlanState.BLOCKED, extracted)
        raw_paths = extracted

    if not raw_paths:
        return _result(
            identity, turn, CodingResultArchivePlanState.EMPTY, CodingResultArchivePlanReason.NO_USER_FILES
        )
    if len(raw_paths) > MAX_ARCHIVE_FILES:
        return _result(
            identity, turn, CodingResultArchivePlanState.BLOCKED, CodingResultArchivePlanReason.FILE_LIMIT
        )

    seen: set[str] = set()
    for path in raw_paths:
        if _SECRET_NAME_RE.search(path) is not None:
            return _result(
                identity,
                turn,
                CodingResultArchivePlanState.BLOCKED,
                CodingResultArchivePlanReason.SECRET_NAME,
            )
        folded = unicodedata.normalize("NFC", path).casefold()
        if folded in seen:
            return _result(
                identity,
                turn,
                CodingResultArchivePlanState.BLOCKED,
                CodingResultArchivePlanReason.CASEFOLD_COLLISION,
            )
        seen.add(folded)

    try:
        visible = select_user_result_files(raw_paths)
        if len(visible) > MAX_ARCHIVE_FILES:
            return _result(
                identity, turn, CodingResultArchivePlanState.BLOCKED, CodingResultArchivePlanReason.FILE_LIMIT
            )
        carrier = select_operation_result_carrier(visible, archive_requested=archive_requested)
    except EngineerResultPolicyError as exc:
        return _result(identity, turn, CodingResultArchivePlanState.BLOCKED, _blocked_from_policy(exc))

    planned = tuple(item.relative_path for item in carrier.files)
    if carrier.carrier is EngineerResultCarrierKind.TEXT:
        return _result(
            identity, turn, CodingResultArchivePlanState.EMPTY, CodingResultArchivePlanReason.NO_USER_FILES
        )
    if carrier.carrier is EngineerResultCarrierKind.FILE:
        return _result(
            identity,
            turn,
            CodingResultArchivePlanState.FILE,
            CodingResultArchivePlanReason.ONE_USER_FILE,
            planned,
        )
    return _result(
        identity,
        turn,
        CodingResultArchivePlanState.ARCHIVE,
        CodingResultArchivePlanReason.MULTIPLE_USER_FILES,
        planned,
    )


plan_coding_result_archive = build_coding_result_archive_plan

__all__ = [
    "CODING_RESULT_ARCHIVE_FILENAME",
    "CODING_RESULT_ARCHIVE_PLAN_SCHEMA",
    "MAX_ARCHIVE_FILES",
    "CodingResultArchivePlanError",
    "CodingResultArchivePlanReason",
    "CodingResultArchivePlanState",
    "CodingResultArchivePlanV1",
    "build_coding_result_archive_plan",
    "plan_coding_result_archive",
]
