"""Pure workspace binding facts for an isolated Coding worker.

The contract binds one relative workspace and one relative export location to
one operation.  It never creates, opens, resolves, or stats a path.  A digest
is only validated; it is never computed here.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

CODING_WORKER_WORKSPACE_SCHEMA = "friday.coding-worker-workspace.v1"
MAX_WORKSPACE_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_OPERATION_ID_CHARS = 128
MAX_PROJECT_ROOT_CHARS = 4_096
MAX_RELATIVE_PATH_CHARS = 4_096
SHA256_HEX_LENGTH = 64

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MISSING = object()


class CodingWorkerWorkspaceError(ValueError):
    """A workspace fact or directly constructed result is malformed."""


class CodingWorkerWorkspaceState(StrEnum):
    """Closed outcomes for one operation workspace binding."""

    EMPTY = "empty"
    BOUND = "bound"
    BLOCKED = "blocked"


class CodingWorkerWorkspaceReason(StrEnum):
    """Non-sensitive reason for one workspace outcome."""

    NO_FACTS = "no_facts"
    WORKSPACE_BOUND = "workspace_bound"
    MISSING_OPERATION_ID = "missing_operation_id"
    MISSING_PROJECT_ROOT = "missing_project_root"
    MISSING_WORKSPACE_PATH = "missing_workspace_path"
    MISSING_INPUT_SNAPSHOT = "missing_input_snapshot"
    MISSING_EXPORT_PATH = "missing_export_path"
    MULTIPLE_WORKSPACES = "multiple_workspaces"
    INVALID_PROJECT_ROOT = "invalid_project_root"
    ABSOLUTE_WORKSPACE_PATH = "absolute_workspace_path"
    WORKSPACE_TRAVERSAL = "workspace_traversal"
    ABSOLUTE_EXPORT_PATH = "absolute_export_path"
    EXPORT_TRAVERSAL = "export_traversal"
    INVALID_SNAPSHOT = "invalid_snapshot"
    INVALID_FACTS = "invalid_facts"


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        raise CodingWorkerWorkspaceError(f"{field} must be a bounded opaque identifier")
    return cast(str, value)


def _text(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum or value != value.strip():
        raise CodingWorkerWorkspaceError(f"{field} must be bounded text")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise CodingWorkerWorkspaceError(f"{field} contains a control character")
    return cast(str, value)


def _state(value: object) -> CodingWorkerWorkspaceState:
    if isinstance(value, CodingWorkerWorkspaceState):
        return value
    if type(value) is not str:
        raise CodingWorkerWorkspaceError("workspace must be a closed value")
    try:
        return CodingWorkerWorkspaceState(value.strip().casefold())
    except ValueError as exc:
        raise CodingWorkerWorkspaceError("workspace must be a closed value") from exc


def _reason(value: object) -> CodingWorkerWorkspaceReason:
    if isinstance(value, CodingWorkerWorkspaceReason):
        return value
    if type(value) is not str:
        raise CodingWorkerWorkspaceError("workspace reason must be a closed value")
    try:
        return CodingWorkerWorkspaceReason(value.strip().casefold())
    except ValueError as exc:
        raise CodingWorkerWorkspaceError("workspace reason must be a closed value") from exc


def _root(value: object) -> str:
    root = _text(value, field="project_root", maximum=MAX_PROJECT_ROOT_CHARS)
    if _DRIVE_RE.match(root) is not None:
        normalized = root.replace("\\", "/")
        if not normalized[2:].startswith("/"):
            raise CodingWorkerWorkspaceError("project_root must be absolute when drive-qualified")
    components = tuple(part for part in re.split(r"[/\\]", root) if part)
    if not components or any(part in {".", ".."} for part in components):
        raise CodingWorkerWorkspaceError("project_root contains traversal")
    return root


def _relative_path(value: object, *, field: str) -> str:
    path = _text(value, field=field, maximum=MAX_RELATIVE_PATH_CHARS)
    if path.startswith(("/", "\\")) or _DRIVE_RE.match(path) is not None:
        raise CodingWorkerWorkspaceError(f"{field} must be relative")
    components = tuple(part for part in re.split(r"[/\\]", path) if part)
    if not components:
        raise CodingWorkerWorkspaceError(f"{field} must not be empty")
    if any(part in {".", ".."} for part in components):
        raise CodingWorkerWorkspaceError(f"{field} contains traversal")
    return "/".join(components)


def _snapshot(value: object) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise CodingWorkerWorkspaceError("input_snapshot_sha256 must be lowercase SHA-256 hex")
    return cast(str, value)


@dataclass(frozen=True, slots=True)
class CodingWorkerWorkspaceFactsV1:
    """Caller-supplied facts for exactly one operation workspace."""

    operation_id: str | None = None
    project_root: str | None = None
    workspace_path: str | None = None
    input_snapshot_sha256: str | None = None
    export_path: str | None = None
    workspace_count: int | None = None


@dataclass(frozen=True, slots=True)
class CodingWorkerWorkspaceV1:
    """Immutable relative workspace binding."""

    workspace_id: str
    authenticated_turn_id: str
    workspace: CodingWorkerWorkspaceState
    operation_id: str | None
    project_root: str | None
    workspace_path: str | None
    input_snapshot_sha256: str | None
    export_path: str | None
    reason: CodingWorkerWorkspaceReason

    def __post_init__(self) -> None:
        _identifier(self.workspace_id, field="workspace_id", maximum=MAX_WORKSPACE_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        workspace = _state(self.workspace)
        reason = _reason(self.reason)
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "reason", reason)
        values = (
            self.operation_id,
            self.project_root,
            self.workspace_path,
            self.input_snapshot_sha256,
            self.export_path,
        )
        if workspace is not CodingWorkerWorkspaceState.BOUND:
            if any(value is not None for value in values):
                raise CodingWorkerWorkspaceError("empty or blocked workspace cannot expose paths")
            return
        if any(value is None for value in values):
            raise CodingWorkerWorkspaceError("bound workspace needs all workspace facts")
        _identifier(self.operation_id, field="operation_id", maximum=MAX_OPERATION_ID_CHARS)
        _root(self.project_root)
        _relative_path(self.workspace_path, field="workspace_path")
        _snapshot(self.input_snapshot_sha256)
        _relative_path(self.export_path, field="export_path")

    @property
    def state(self) -> CodingWorkerWorkspaceState:
        return self.workspace

    @property
    def binding(self) -> CodingWorkerWorkspaceState:
        return self.workspace

    @property
    def closed_workspace(self) -> CodingWorkerWorkspaceState:
        return self.workspace

    @property
    def decision(self) -> CodingWorkerWorkspaceState:
        return self.workspace

    @property
    def relative_workspace_path(self) -> str | None:
        return self.workspace_path

    @property
    def relative_export_path(self) -> str | None:
        return self.export_path

    @property
    def closed_reason(self) -> CodingWorkerWorkspaceReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_WORKER_WORKSPACE_SCHEMA,
            "workspace_id": self.workspace_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "workspace": self.workspace.value,
            "operation_id": self.operation_id,
            "project_root": self.project_root,
            "workspace_path": self.workspace_path,
            "input_snapshot_sha256": self.input_snapshot_sha256,
            "export_path": self.export_path,
            "reason": self.reason.value,
        }


WorkerWorkspaceState = CodingWorkerWorkspaceState
WorkerWorkspaceReason = CodingWorkerWorkspaceReason
CodingWorkerWorkspaceBinding = CodingWorkerWorkspaceV1
CodingWorkerWorkspaceDecision = CodingWorkerWorkspaceState
CodingWorkerWorkspaceFacts = CodingWorkerWorkspaceFactsV1
CodingWorkerWorkspaceBindingState = CodingWorkerWorkspaceState
CodingWorkerWorkspaceBindingReason = CodingWorkerWorkspaceReason
CodingWorkerWorkspaceBindingFactsV1 = CodingWorkerWorkspaceFactsV1
CODING_WORKER_WORKSPACE_BINDING_SCHEMA = CODING_WORKER_WORKSPACE_SCHEMA


def _mapping_facts(value: Mapping[str, object]) -> tuple[object, object, object, object, object, object]:
    allowed = {
        "schema",
        "workspace_id",
        "authenticated_turn_id",
        "workspace",
        "state",
        "binding",
        "reason",
        "operation_id",
        "operation",
        "project_root",
        "project_root_path",
        "root",
        "workspace_path",
        "path",
        "input_snapshot_sha256",
        "input_snapshot",
        "snapshot_sha256",
        "snapshot_digest",
        "export_path",
        "export",
        "workspace_count",
    }
    if set(value) - allowed:
        raise CodingWorkerWorkspaceError("workspace facts contain unknown fields")
    if value.get("schema", CODING_WORKER_WORKSPACE_SCHEMA) != CODING_WORKER_WORKSPACE_SCHEMA:
        raise CodingWorkerWorkspaceError("workspace schema is invalid")
    operation = value.get("operation_id", value.get("operation", _MISSING))
    root = value.get("project_root", value.get("project_root_path", value.get("root", _MISSING)))
    path = value.get("workspace_path", value.get("path", _MISSING))
    snapshot = value.get(
        "input_snapshot_sha256",
        value.get("input_snapshot", value.get("snapshot_sha256", value.get("snapshot_digest", _MISSING))),
    )
    export = value.get("export_path", value.get("export", _MISSING))
    count = value.get("workspace_count", _MISSING)
    return operation, root, path, snapshot, export, count


def _facts(value: object) -> tuple[object, object, object, object, object, object]:
    if value is None:
        return (_MISSING, _MISSING, _MISSING, _MISSING, _MISSING, _MISSING)
    if isinstance(value, CodingWorkerWorkspaceFactsV1):
        return (
            value.operation_id,
            value.project_root,
            value.workspace_path,
            value.input_snapshot_sha256,
            value.export_path,
            value.workspace_count,
        )
    if isinstance(value, Mapping):
        return _mapping_facts(value)
    raise CodingWorkerWorkspaceError("workspace facts must be a mapping or facts object")


def _result(
    workspace_id: str,
    authenticated_turn_id: str,
    workspace: CodingWorkerWorkspaceState,
    reason: CodingWorkerWorkspaceReason,
    *,
    operation_id: str | None = None,
    project_root: str | None = None,
    workspace_path: str | None = None,
    snapshot: str | None = None,
    export_path: str | None = None,
) -> CodingWorkerWorkspaceV1:
    if workspace is not CodingWorkerWorkspaceState.BOUND:
        operation_id = project_root = workspace_path = snapshot = export_path = None
    return CodingWorkerWorkspaceV1(
        workspace_id=workspace_id,
        authenticated_turn_id=authenticated_turn_id,
        workspace=workspace,
        operation_id=operation_id,
        project_root=project_root,
        workspace_path=workspace_path,
        input_snapshot_sha256=snapshot,
        export_path=export_path,
        reason=reason,
    )


def build_coding_worker_workspace(
    workspace_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    facts: CodingWorkerWorkspaceFactsV1 | Mapping[str, object] | None = None,
    *,
    operation_id: object = _MISSING,
    project_root: object = _MISSING,
    workspace_path: object = _MISSING,
    input_snapshot_sha256: object = _MISSING,
    export_path: object = _MISSING,
    workspace_count: object = _MISSING,
) -> CodingWorkerWorkspaceV1:
    """Bind exactly one relative workspace from supplied facts."""

    if isinstance(workspace_id, Mapping):
        raw = workspace_id
        workspace_id = raw.get("workspace_id", "workspace:worker")
        authenticated_turn_id = raw.get("authenticated_turn_id", authenticated_turn_id)
        if facts is not None or any(
            value is not _MISSING
            for value in (
                operation_id,
                project_root,
                workspace_path,
                input_snapshot_sha256,
                export_path,
                workspace_count,
            )
        ):
            raise CodingWorkerWorkspaceError("workspace mapping and explicit facts cannot be mixed")
        facts = raw
    _identifier(workspace_id, field="workspace_id", maximum=MAX_WORKSPACE_ID_CHARS)
    _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    try:
        explicit = (
            operation_id,
            project_root,
            workspace_path,
            input_snapshot_sha256,
            export_path,
            workspace_count,
        )
        raw_facts = explicit if any(value is not _MISSING for value in explicit) else _facts(facts)
        if any(value is not _MISSING for value in explicit) and facts is not None:
            raise CodingWorkerWorkspaceError("facts and explicit workspace facts cannot both be supplied")
    except CodingWorkerWorkspaceError:
        return _result(
            cast(str, workspace_id),
            cast(str, authenticated_turn_id),
            CodingWorkerWorkspaceState.BLOCKED,
            CodingWorkerWorkspaceReason.INVALID_FACTS,
        )
    if all(value is _MISSING or value is None for value in raw_facts):
        return _result(
            cast(str, workspace_id),
            cast(str, authenticated_turn_id),
            CodingWorkerWorkspaceState.EMPTY,
            CodingWorkerWorkspaceReason.NO_FACTS,
        )
    operation_fact, root_fact, path_fact, snapshot_fact, export_fact, count_fact = raw_facts
    if count_fact not in (_MISSING, None, 1):
        return _result(
            cast(str, workspace_id),
            cast(str, authenticated_turn_id),
            CodingWorkerWorkspaceState.BLOCKED,
            CodingWorkerWorkspaceReason.MULTIPLE_WORKSPACES,
        )
    if operation_fact is _MISSING or operation_fact is None:
        reason = CodingWorkerWorkspaceReason.MISSING_OPERATION_ID
    elif root_fact is _MISSING or root_fact is None:
        reason = CodingWorkerWorkspaceReason.MISSING_PROJECT_ROOT
    elif path_fact is _MISSING or path_fact is None:
        reason = CodingWorkerWorkspaceReason.MISSING_WORKSPACE_PATH
    elif snapshot_fact is _MISSING or snapshot_fact is None:
        reason = CodingWorkerWorkspaceReason.MISSING_INPUT_SNAPSHOT
    elif export_fact is _MISSING or export_fact is None:
        reason = CodingWorkerWorkspaceReason.MISSING_EXPORT_PATH
    else:
        reason = None
    if reason is not None:
        return _result(
            cast(str, workspace_id),
            cast(str, authenticated_turn_id),
            CodingWorkerWorkspaceState.BLOCKED,
            reason,
        )
    try:
        operation_value = _identifier(operation_fact, field="operation_id", maximum=MAX_OPERATION_ID_CHARS)
        root_value = _root(root_fact)
        path_value = _relative_path(path_fact, field="workspace_path")
        snapshot_value = _snapshot(snapshot_fact)
        export_value = _relative_path(export_fact, field="export_path")
    except CodingWorkerWorkspaceError as exc:
        message = str(exc)
        if "project_root" in message:
            reason = CodingWorkerWorkspaceReason.INVALID_PROJECT_ROOT
        elif "workspace_path" in message and "relative" in message:
            reason = CodingWorkerWorkspaceReason.ABSOLUTE_WORKSPACE_PATH
        elif "workspace_path" in message:
            reason = CodingWorkerWorkspaceReason.WORKSPACE_TRAVERSAL
        elif "export_path" in message and "relative" in message:
            reason = CodingWorkerWorkspaceReason.ABSOLUTE_EXPORT_PATH
        elif "export_path" in message:
            reason = CodingWorkerWorkspaceReason.EXPORT_TRAVERSAL
        elif "snapshot" in message:
            reason = CodingWorkerWorkspaceReason.INVALID_SNAPSHOT
        else:
            reason = CodingWorkerWorkspaceReason.INVALID_FACTS
        return _result(
            cast(str, workspace_id),
            cast(str, authenticated_turn_id),
            CodingWorkerWorkspaceState.BLOCKED,
            reason,
        )
    return _result(
        cast(str, workspace_id),
        cast(str, authenticated_turn_id),
        CodingWorkerWorkspaceState.BOUND,
        CodingWorkerWorkspaceReason.WORKSPACE_BOUND,
        operation_id=operation_value,
        project_root=root_value,
        workspace_path=path_value,
        snapshot=snapshot_value,
        export_path=export_value,
    )


build_coding_worker_workspace_binding = build_coding_worker_workspace


def validate_coding_worker_workspace(value: Mapping[str, object]) -> bool:
    """Return whether a mapping is a valid serialized workspace result."""

    try:
        if value.get("schema") != CODING_WORKER_WORKSPACE_SCHEMA:
            return False
        workspace_id = cast(str, value.get("workspace_id"))
        turn = cast(str, value.get("authenticated_turn_id"))
        state_value = value.get("workspace", value.get("binding", value.get("state")))
        if state_value in {
            CodingWorkerWorkspaceState.EMPTY.value,
            CodingWorkerWorkspaceState.BLOCKED.value,
        }:
            state = CodingWorkerWorkspaceState(state_value)
            reason = CodingWorkerWorkspaceReason(cast(str, value.get("reason")))
            result = CodingWorkerWorkspaceV1(workspace_id, turn, state, None, None, None, None, None, reason)
        else:
            result = build_coding_worker_workspace(workspace_id, turn, value)
        return result.to_mapping() == dict(value)
    except (CodingWorkerWorkspaceError, TypeError, ValueError):
        return False
