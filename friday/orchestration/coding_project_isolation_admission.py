"""Pure project-root containment admission for Coding Mode destinations.

The builder checks only caller-supplied path facts.  It performs no stat,
realpath, directory creation, or other filesystem operation.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

CODING_PROJECT_ISOLATION_ADMISSION_SCHEMA = "friday.coding-project-isolation-admission.v1"
MAX_ISOLATION_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_PROJECT_ROOT_CHARS = 4_096
MAX_DESTINATION_CHARS = 4_096

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class CodingProjectIsolationAdmissionError(ValueError):
    """An isolation identity, root fact, or destination is malformed."""


class CodingProjectIsolationAdmissionState(StrEnum):
    """Closed project-isolation outcomes."""

    EMPTY = "empty"
    ADMITTED = "admitted"
    BLOCKED = "blocked"


class CodingProjectIsolationAdmissionReason(StrEnum):
    """Closed reason for one project-isolation outcome."""

    NO_FACTS = "no_facts"
    DESTINATION_WITHIN_PROJECT = "destination_within_project"
    MISSING_PROJECT_ROOT = "missing_project_root"
    MISSING_DESTINATION = "missing_destination"
    PROJECT_ROOT_INVALID = "project_root_invalid"
    DESTINATION_INVALID = "destination_invalid"
    DESTINATION_TRAVERSAL = "destination_traversal"
    ABSOLUTE_DESTINATION = "absolute_destination"
    OUTSIDE_PROJECT_ROOT = "outside_project_root"
    INVALID_FACTS = "invalid_facts"

    ADMISSION_GRANTED = DESTINATION_WITHIN_PROJECT


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise CodingProjectIsolationAdmissionError(f"{field}_{detail}")


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _text(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum or value != value.strip():
        _fail(field, "text")
    if any(unicodedata.category(character).startswith("C") for character in value):
        _fail(field, "control")
    return cast(str, value)


def _state(value: object) -> CodingProjectIsolationAdmissionState:
    try:
        return CodingProjectIsolationAdmissionState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingProjectIsolationAdmissionError("admission_closed") from exc


def _reason(value: object) -> CodingProjectIsolationAdmissionReason:
    try:
        return CodingProjectIsolationAdmissionReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingProjectIsolationAdmissionError("reason_closed") from exc


def _root_parts(value: object) -> tuple[str, ...]:
    root = _text(value, field="project_root", maximum=MAX_PROJECT_ROOT_CHARS)
    if _DRIVE_RE.match(root) is not None:
        root = root.replace("\\", "/")
        prefix = root[:2]
        remainder = root[2:]
        parts = tuple(part for part in remainder.split("/") if part)
        absolute = remainder.startswith("/")
        if not absolute or not parts or any(part in {".", ".."} for part in parts):
            _fail("project_root", "invalid")
        return (prefix, *parts)
    absolute = root.startswith(("/", "\\"))
    parts = tuple(part for part in re.split(r"[/\\]", root) if part)
    if not parts or any(part in {".", ".."} for part in parts):
        _fail("project_root", "invalid")
    if absolute:
        return ("/", *parts)
    return parts


def _destination_parts(value: object) -> tuple[str, ...]:
    destination = _text(value, field="destination", maximum=MAX_DESTINATION_CHARS)
    if destination.startswith(("/", "\\")) or _DRIVE_RE.match(destination) is not None:
        _fail("destination", "absolute")
    parts = tuple(part for part in re.split(r"[/\\]", destination) if part)
    if not parts or any(part in {".", ".."} for part in parts):
        _fail("destination", "traversal")
    return parts


@dataclass(frozen=True, slots=True)
class CodingProjectIsolationFactsV1:
    """Frozen project-root and destination facts supplied by the caller."""

    project_root: str | None = None
    destination: str | None = None


@dataclass(frozen=True, slots=True)
class CodingProjectIsolationAdmissionV1:
    """Immutable admission that a relative destination belongs to a root."""

    isolation_id: str
    authenticated_turn_id: str
    admission: CodingProjectIsolationAdmissionState
    project_root: str | None
    destination: str | None
    reason: CodingProjectIsolationAdmissionReason

    def __post_init__(self) -> None:
        _identifier(self.isolation_id, field="isolation_id", maximum=MAX_ISOLATION_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        admission = _state(self.admission)
        reason = _reason(self.reason)
        object.__setattr__(self, "admission", admission)
        object.__setattr__(self, "reason", reason)
        if admission is CodingProjectIsolationAdmissionState.ADMITTED:
            if self.project_root is None or self.destination is None:
                _fail("admission", "missing_paths")
            _root_parts(self.project_root)
            _destination_parts(self.destination)
        elif self.project_root is not None or self.destination is not None:
            _fail("blocked_or_empty_paths", "exposed")

    @property
    def state(self) -> CodingProjectIsolationAdmissionState:
        return self.admission

    @property
    def closed_admission(self) -> CodingProjectIsolationAdmissionState:
        return self.admission

    @property
    def decision(self) -> CodingProjectIsolationAdmissionState:
        return self.admission

    @property
    def relative_destination(self) -> str | None:
        return self.destination

    @property
    def destination_path(self) -> str | None:
        return self.destination

    @property
    def project_root_path(self) -> str | None:
        return self.project_root

    @property
    def closed_reason(self) -> CodingProjectIsolationAdmissionReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_PROJECT_ISOLATION_ADMISSION_SCHEMA,
            "isolation_id": self.isolation_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "admission": self.admission.value,
            "project_root": self.project_root,
            "destination": self.destination,
            "reason": self.reason.value,
        }


IsolationAdmissionState = CodingProjectIsolationAdmissionState
IsolationAdmissionReason = CodingProjectIsolationAdmissionReason
CodingProjectIsolationAdmission = CodingProjectIsolationAdmissionV1
CodingProjectIsolationAdmissionDecision = CodingProjectIsolationAdmissionState
ProjectIsolationFacts = CodingProjectIsolationFactsV1


def _facts(value: object) -> tuple[object, object]:
    if isinstance(value, CodingProjectIsolationFactsV1):
        return value.project_root, value.destination
    if not isinstance(value, Mapping):
        _fail("facts", "type")
    allowed = {
        "project_root",
        "root",
        "project_root_path",
        "destination",
        "destination_path",
        "target",
    }
    if set(value) - allowed:
        _fail("facts", "unknown_fields")
    root = value.get("project_root", value.get("root", value.get("project_root_path")))
    destination = value.get("destination", value.get("destination_path", value.get("target")))
    return root, destination


def _known_mapping_keys(raw: Mapping[str, Any]) -> None:
    known = {
        "schema",
        "isolation_id",
        "authenticated_turn_id",
        "facts",
        "project_root",
        "root",
        "project_root_path",
        "destination",
        "destination_path",
        "target",
        "admission",
        "state",
        "reason",
    }
    if set(raw) - known:
        _fail("isolation", "unknown_fields")


def _result(
    isolation_id: str,
    authenticated_turn_id: str,
    admission: CodingProjectIsolationAdmissionState,
    reason: CodingProjectIsolationAdmissionReason,
    *,
    project_root: str | None = None,
    destination: str | None = None,
) -> CodingProjectIsolationAdmissionV1:
    if admission is not CodingProjectIsolationAdmissionState.ADMITTED:
        project_root = None
        destination = None
    return CodingProjectIsolationAdmissionV1(
        isolation_id=isolation_id,
        authenticated_turn_id=authenticated_turn_id,
        admission=admission,
        project_root=project_root,
        destination=destination,
        reason=reason,
    )


def build_coding_project_isolation_admission(
    isolation_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    facts: CodingProjectIsolationFactsV1 | Mapping[str, object] | None = None,
    *,
    project_root: object = None,
    destination: object = None,
) -> CodingProjectIsolationAdmissionV1:
    """Admit a relative destination under the supplied project-root fact."""

    if isinstance(isolation_id, Mapping):
        raw = isolation_id
        _known_mapping_keys(raw)
        if (
            raw.get("schema", CODING_PROJECT_ISOLATION_ADMISSION_SCHEMA)
            != CODING_PROJECT_ISOLATION_ADMISSION_SCHEMA
        ):
            _fail("schema")
        output_keys = {"admission", "state", "reason"}
        fact_keys = {
            "facts",
            "root",
            "project_root_path",
            "destination_path",
            "target",
        }
        if output_keys.intersection(raw) and fact_keys.intersection(raw):
            _fail("isolation", "duplicate_representations")
        if output_keys.intersection(raw):
            return CodingProjectIsolationAdmissionV1(
                isolation_id=cast(str, raw.get("isolation_id")),
                authenticated_turn_id=cast(str, raw.get("authenticated_turn_id")),
                admission=cast(CodingProjectIsolationAdmissionState, raw.get("admission", raw.get("state"))),
                project_root=cast(str | None, raw.get("project_root")),
                destination=cast(str | None, raw.get("destination")),
                reason=cast(CodingProjectIsolationAdmissionReason, raw.get("reason")),
            )
        isolation_id = cast(str, raw.get("isolation_id"))
        authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
        if "facts" in raw:
            facts = cast(CodingProjectIsolationFactsV1 | Mapping[str, object], raw["facts"])
        else:
            project_root = raw.get("project_root", raw.get("root", raw.get("project_root_path")))
            destination = raw.get("destination", raw.get("destination_path", raw.get("target")))

    isolation_key = _identifier(isolation_id, field="isolation_id", maximum=MAX_ISOLATION_ID_CHARS)
    turn_key = _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    try:
        if facts is not None:
            if project_root is not None or destination is not None:
                _fail("facts", "duplicate_arguments")
            project_root, destination = _facts(facts)
        if project_root is None and destination is None:
            return _result(
                isolation_key,
                turn_key,
                CodingProjectIsolationAdmissionState.EMPTY,
                CodingProjectIsolationAdmissionReason.NO_FACTS,
            )
        if project_root is None:
            reason = CodingProjectIsolationAdmissionReason.MISSING_PROJECT_ROOT
        elif destination is None:
            reason = CodingProjectIsolationAdmissionReason.MISSING_DESTINATION
        else:
            root_parts = _root_parts(project_root)
            destination_parts = _destination_parts(destination)
            root = "/" + "/".join(root_parts[1:]) if root_parts[0] == "/" else "/".join(root_parts)
            relative = "/".join(destination_parts)
            return _result(
                isolation_key,
                turn_key,
                CodingProjectIsolationAdmissionState.ADMITTED,
                CodingProjectIsolationAdmissionReason.DESTINATION_WITHIN_PROJECT,
                project_root=root,
                destination=relative,
            )
    except CodingProjectIsolationAdmissionError as exc:
        message = str(exc)
        if "destination_absolute" in message:
            reason = CodingProjectIsolationAdmissionReason.ABSOLUTE_DESTINATION
        elif "destination_traversal" in message:
            reason = CodingProjectIsolationAdmissionReason.DESTINATION_TRAVERSAL
        elif "project_root" in message:
            reason = CodingProjectIsolationAdmissionReason.PROJECT_ROOT_INVALID
        elif "destination" in message:
            reason = CodingProjectIsolationAdmissionReason.DESTINATION_INVALID
        else:
            reason = CodingProjectIsolationAdmissionReason.INVALID_FACTS
    return _result(
        isolation_key,
        turn_key,
        CodingProjectIsolationAdmissionState.BLOCKED,
        reason,
    )


def validate_coding_project_isolation_admission(value: object) -> bool:
    """Return whether a frozen isolation result or serialized result is valid."""

    try:
        if isinstance(value, CodingProjectIsolationAdmissionV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping):
            return False
        _known_mapping_keys(value)
        if value.get("schema") != CODING_PROJECT_ISOLATION_ADMISSION_SCHEMA:
            return False
        required = {
            "schema",
            "isolation_id",
            "authenticated_turn_id",
            "admission",
            "project_root",
            "destination",
            "reason",
        }
        if set(value) != required:
            return False
        return (
            CodingProjectIsolationAdmissionV1(
                isolation_id=cast(str, value.get("isolation_id")),
                authenticated_turn_id=cast(str, value.get("authenticated_turn_id")),
                admission=cast(CodingProjectIsolationAdmissionState, value.get("admission")),
                project_root=cast(str | None, value.get("project_root")),
                destination=cast(str | None, value.get("destination")),
                reason=cast(CodingProjectIsolationAdmissionReason, value.get("reason")),
            )
            is not None
        )
    except (TypeError, ValueError):
        return False


admit_project_destination = build_coding_project_isolation_admission
build_project_isolation_admission = build_coding_project_isolation_admission
validate_project_isolation_admission = validate_coding_project_isolation_admission


__all__ = [
    "CODING_PROJECT_ISOLATION_ADMISSION_SCHEMA",
    "MAX_AUTHENTICATED_TURN_ID_CHARS",
    "MAX_DESTINATION_CHARS",
    "MAX_ISOLATION_ID_CHARS",
    "MAX_PROJECT_ROOT_CHARS",
    "CodingProjectIsolationAdmission",
    "CodingProjectIsolationAdmissionDecision",
    "CodingProjectIsolationAdmissionError",
    "CodingProjectIsolationAdmissionReason",
    "CodingProjectIsolationAdmissionState",
    "CodingProjectIsolationAdmissionV1",
    "CodingProjectIsolationFactsV1",
    "IsolationAdmissionReason",
    "IsolationAdmissionState",
    "ProjectIsolationFacts",
    "admit_project_destination",
    "build_coding_project_isolation_admission",
    "build_project_isolation_admission",
    "validate_coding_project_isolation_admission",
    "validate_project_isolation_admission",
]
