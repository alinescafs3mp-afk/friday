"""Pure exact-revision project identity for Coding Mode.

The contract consumes supplied identity facts only.  It never calls git,
creates directories, reads files, or wires a coding worker.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class CodingProjectIdentityError(ValueError):
    """The identity or supplied project facts are invalid."""


class CodingProjectIdentityState(StrEnum):
    """Closed project-identity outcomes."""

    EMPTY = "empty"
    IDENTIFIED = "identified"
    BLOCKED = "blocked"


class CodingProjectIdentityReason(StrEnum):
    """Closed short reasons for one project-identity result."""

    NO_FACTS = "no_facts"
    IDENTIFIED = "identified"
    MISSING_PROJECT_ID = "missing_project_id"
    MISSING_REVISION_SELECTOR = "missing_revision_selector"
    RECENCY_REVISION_SELECTOR = "recency_revision_selector"
    INVALID_FACTS = "invalid_facts"


@dataclass(frozen=True, slots=True)
class CodingProjectIdentityFactsV1:
    """Frozen input facts for one project and one exact revision selector."""

    project_id: str | None = None
    revision_selector: str | None = None


@dataclass(frozen=True, slots=True)
class CodingProjectIdentityV1:
    """Frozen body-free project identity for one authenticated turn."""

    identity_id: str
    authenticated_turn_id: str
    identity: CodingProjectIdentityState
    project_id: str | None
    revision_selector: str | None
    reason: CodingProjectIdentityReason

    @property
    def state(self) -> CodingProjectIdentityState:
        return self.identity

    @property
    def closed_identity(self) -> CodingProjectIdentityState:
        return self.identity

    @property
    def decision(self) -> CodingProjectIdentityState:
        return self.identity

    @property
    def closed_reason(self) -> CodingProjectIdentityReason:
        return self.reason

    def __post_init__(self) -> None:
        _identity(self.identity_id, field="identity_id")
        _identity(self.authenticated_turn_id, field="authenticated_turn_id")
        identity = _state(self.identity)
        reason = _reason(self.reason)
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "reason", reason)
        if identity is CodingProjectIdentityState.BLOCKED and (
            self.project_id is not None or self.revision_selector is not None
        ):
            raise CodingProjectIdentityError("blocked identity cannot expose project facts")
        if identity is CodingProjectIdentityState.EMPTY and (
            self.project_id is not None or self.revision_selector is not None
        ):
            raise CodingProjectIdentityError("empty identity cannot expose project facts")
        if identity is CodingProjectIdentityState.IDENTIFIED:
            if self.project_id is None or self.revision_selector is None:
                raise CodingProjectIdentityError("identified identity needs both project facts")
            _identity(self.project_id, field="project_id")
            _identity(self.revision_selector, field="revision_selector")
            if _is_recency_selector(self.revision_selector):
                raise CodingProjectIdentityError("identified identity cannot use a recency selector")


ProjectIdentityState = CodingProjectIdentityState
ProjectIdentityReason = CodingProjectIdentityReason
CodingProjectIdentity = CodingProjectIdentityV1
CodingProjectIdentityDecision = CodingProjectIdentityState
CodingProjectFacts = CodingProjectIdentityFactsV1


_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MISSING = object()
_RECENCY_SELECTORS = frozenset({"latest", "head", "newest", "current", ""})


def _identity(value: object, *, field: str) -> str:
    if type(value) is not str or _IDENTITY_RE.fullmatch(value) is None:
        raise CodingProjectIdentityError(f"{field} must be a bounded opaque exact identifier")
    return value


def _state(value: object) -> CodingProjectIdentityState:
    if isinstance(value, CodingProjectIdentityState):
        return value
    if type(value) is not str:
        raise CodingProjectIdentityError("identity must be a closed value")
    try:
        return CodingProjectIdentityState(value.strip().casefold())
    except ValueError as exc:
        raise CodingProjectIdentityError("unknown identity value") from exc


def _reason(value: object) -> CodingProjectIdentityReason:
    if isinstance(value, CodingProjectIdentityReason):
        return value
    if type(value) is not str:
        raise CodingProjectIdentityError("reason must be a closed value")
    try:
        return CodingProjectIdentityReason(value.strip().casefold())
    except ValueError as exc:
        raise CodingProjectIdentityError("unknown identity reason") from exc


def _is_recency_selector(value: object) -> bool:
    return type(value) is str and value.strip().casefold() in _RECENCY_SELECTORS


def _mapping_facts(value: Mapping[str, object]) -> tuple[object, object]:
    allowed = {
        "project_id",
        "project",
        "revision_selector",
        "revision",
        "revision_id",
    }
    if set(value) - allowed:
        raise CodingProjectIdentityError("identity facts contain unknown fields")
    project_id = value.get("project_id", value.get("project", _MISSING))
    revision_selector = value.get(
        "revision_selector",
        value.get("revision", value.get("revision_id", _MISSING)),
    )
    return project_id, revision_selector


def _facts(value: object) -> tuple[object, object]:
    if value is None:
        return _MISSING, _MISSING
    if isinstance(value, CodingProjectIdentityFactsV1):
        return value.project_id, value.revision_selector
    if isinstance(value, Mapping):
        return _mapping_facts(value)
    raise CodingProjectIdentityError("identity facts must be a mapping or facts object")


def _result(
    identity_id: str,
    authenticated_turn_id: str,
    identity: CodingProjectIdentityState,
    reason: CodingProjectIdentityReason,
    *,
    project_id: str | None = None,
    revision_selector: str | None = None,
) -> CodingProjectIdentityV1:
    if identity is not CodingProjectIdentityState.IDENTIFIED:
        project_id = None
        revision_selector = None
    return CodingProjectIdentityV1(
        identity_id=identity_id,
        authenticated_turn_id=authenticated_turn_id,
        identity=identity,
        project_id=project_id,
        revision_selector=revision_selector,
        reason=reason,
    )


def build_coding_project_identity(
    identity_id: str,
    authenticated_turn_id: str,
    facts: CodingProjectIdentityFactsV1 | Mapping[str, object] | None = None,
    revision_selector: object = _MISSING,
    *,
    project_id: object = _MISSING,
) -> CodingProjectIdentityV1:
    """Build a project identity from exact, already-supplied facts.

    ``facts`` accepts the frozen input dataclass or a mapping.  For callers
    using positional project and revision values, a string ``facts`` value is
    treated as ``project_id`` when ``revision_selector`` is also supplied.
    """

    _identity(identity_id, field="identity_id")
    _identity(authenticated_turn_id, field="authenticated_turn_id")
    try:
        if isinstance(facts, str) and revision_selector is not _MISSING:
            if project_id is not _MISSING:
                raise CodingProjectIdentityError("project_id was supplied twice")
            project_fact, revision_fact = facts, revision_selector
        elif project_id is not _MISSING or revision_selector is not _MISSING:
            if facts is not None:
                raise CodingProjectIdentityError("facts and explicit project facts cannot both be supplied")
            project_fact = project_id
            revision_fact = revision_selector
        else:
            project_fact, revision_fact = _facts(facts)
    except CodingProjectIdentityError:
        return _result(
            identity_id,
            authenticated_turn_id,
            CodingProjectIdentityState.BLOCKED,
            CodingProjectIdentityReason.INVALID_FACTS,
        )
    except (TypeError, ValueError):
        return _result(
            identity_id,
            authenticated_turn_id,
            CodingProjectIdentityState.BLOCKED,
            CodingProjectIdentityReason.INVALID_FACTS,
        )

    project_absent = project_fact is _MISSING or project_fact is None
    revision_absent = revision_fact is _MISSING or revision_fact is None
    if project_absent and revision_absent:
        return _result(
            identity_id,
            authenticated_turn_id,
            CodingProjectIdentityState.EMPTY,
            CodingProjectIdentityReason.NO_FACTS,
        )
    if _is_recency_selector(revision_fact):
        return _result(
            identity_id,
            authenticated_turn_id,
            CodingProjectIdentityState.BLOCKED,
            CodingProjectIdentityReason.RECENCY_REVISION_SELECTOR,
        )
    if project_absent:
        return _result(
            identity_id,
            authenticated_turn_id,
            CodingProjectIdentityState.BLOCKED,
            CodingProjectIdentityReason.MISSING_PROJECT_ID,
        )
    if revision_absent:
        return _result(
            identity_id,
            authenticated_turn_id,
            CodingProjectIdentityState.BLOCKED,
            CodingProjectIdentityReason.MISSING_REVISION_SELECTOR,
        )
    try:
        project_value = _identity(project_fact, field="project_id")
        revision_value = _identity(revision_fact, field="revision_selector")
    except CodingProjectIdentityError:
        return _result(
            identity_id,
            authenticated_turn_id,
            CodingProjectIdentityState.BLOCKED,
            CodingProjectIdentityReason.INVALID_FACTS,
        )
    if _is_recency_selector(revision_value):
        return _result(
            identity_id,
            authenticated_turn_id,
            CodingProjectIdentityState.BLOCKED,
            CodingProjectIdentityReason.RECENCY_REVISION_SELECTOR,
        )
    return _result(
        identity_id,
        authenticated_turn_id,
        CodingProjectIdentityState.IDENTIFIED,
        CodingProjectIdentityReason.IDENTIFIED,
        project_id=project_value,
        revision_selector=revision_value,
    )


def identify_coding_project(
    identity_id: str,
    authenticated_turn_id: str,
    facts: CodingProjectIdentityFactsV1 | Mapping[str, object] | None = None,
    revision_selector: object = _MISSING,
    *,
    project_id: object = _MISSING,
) -> CodingProjectIdentityV1:
    """Alias for the explicit Coding Mode identity builder."""

    return build_coding_project_identity(
        identity_id,
        authenticated_turn_id,
        facts,
        revision_selector,
        project_id=project_id,
    )


assess_coding_project_identity = build_coding_project_identity
resolve_coding_project_identity = build_coding_project_identity
evaluate_coding_project_identity = build_coding_project_identity


class CodingProjectIdentityPolicy:
    """Stateless façade for orchestration dependency injection."""

    @staticmethod
    def build(
        identity_id: str,
        authenticated_turn_id: str,
        facts: CodingProjectIdentityFactsV1 | Mapping[str, object] | None = None,
        revision_selector: object = _MISSING,
        *,
        project_id: object = _MISSING,
    ) -> CodingProjectIdentityV1:
        return build_coding_project_identity(
            identity_id,
            authenticated_turn_id,
            facts,
            revision_selector,
            project_id=project_id,
        )


__all__ = (
    "CodingProjectFacts",
    "CodingProjectIdentity",
    "CodingProjectIdentityDecision",
    "CodingProjectIdentityError",
    "CodingProjectIdentityFactsV1",
    "CodingProjectIdentityPolicy",
    "CodingProjectIdentityReason",
    "CodingProjectIdentityState",
    "CodingProjectIdentityV1",
    "ProjectIdentityReason",
    "ProjectIdentityState",
    "assess_coding_project_identity",
    "build_coding_project_identity",
    "evaluate_coding_project_identity",
    "identify_coding_project",
    "resolve_coding_project_identity",
)
