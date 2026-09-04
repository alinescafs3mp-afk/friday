"""Read-only shared operation view composed from supplied durable facts.

The view joins one already-admitted operation-progress projection with small
body-free summaries.  It does not query stores, execute work, schedule a
retry, or publish a result.  Every component remains immutable and a blocked
component absorbs the composed view.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from friday.orchestration.operation_progress import (
    OperationMode,
    OperationProgressError,
    OperationProgressProjection,
    OperationStep,
    build_operation_progress,
)
from friday.orchestration.shared_operation_artifacts import (
    SHARED_OPERATION_ARTIFACTS_SCHEMA,
    SharedOperationArtifactsFactsV1,
    SharedOperationArtifactsV1,
    build_shared_operation_artifacts,
)
from friday.orchestration.shared_operation_binding import (
    SHARED_OPERATION_BINDING_SCHEMA,
    SharedOperationBindingFactsV1,
    SharedOperationBindingV1,
    build_shared_operation_binding,
)
from friday.orchestration.shared_operation_capability import (
    SHARED_OPERATION_CAPABILITY_SCHEMA,
    SharedOperationCapabilityFactsV1,
    SharedOperationCapabilityState,
    SharedOperationCapabilityV1,
    build_shared_operation_capability,
)
from friday.orchestration.shared_operation_secondary import (
    SHARED_OPERATION_SECONDARY_SCHEMA,
    SharedOperationSecondaryFactsV1,
    SharedOperationSecondaryState,
    SharedOperationSecondaryV1,
    build_shared_operation_secondary,
)

SHARED_OPERATION_VIEW_SCHEMA = "friday.shared-operation-view.v1"
MAX_VIEW_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_AUTHORIZED_SOURCE_COUNT = 32
MAX_HOSTNAME_CHARS = 253
MAX_PENDING_OWNER_CHARS = 32
MAX_OWNER_CLAIMS = 8

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_HOST_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,31}\Z")
_MISSING = object()


class SharedOperationViewError(ValueError):
    """A shared-operation view identity, fact, or composition is malformed."""


class SharedOperationViewState(StrEnum):
    EMPTY = "empty"
    PROJECTED = "projected"
    BLOCKED = "blocked"


class SharedOperationViewReason(StrEnum):
    NO_FACTS = "no_facts"
    PROJECTED = "projected"
    PROGRESS_INVALID = "progress_invalid"
    COMPONENT_BLOCKED = "component_blocked"
    IDENTITY_MISMATCH = "identity_mismatch"
    SOURCE_INVALID = "source_invalid"
    PENDING_OWNER_INVALID = "pending_owner_invalid"
    SECONDARY_OWNERSHIP = "secondary_ownership"
    MULTIPLE_EFFECT_OWNERS = "multiple_effect_owners"
    MULTIPLE_PUBLISHERS = "multiple_publishers"
    INVALID_FACTS = "invalid_facts"


class SharedOperationPendingWorkOwner(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SharedOperationViewFactsV1:
    """Optional, already-supplied facts used to compose a view."""

    operation_progress: OperationProgressProjection | Mapping[str, Any] | None = None
    binding: SharedOperationBindingV1 | SharedOperationBindingFactsV1 | Mapping[str, Any] | None = None
    authorized_source_summary: tuple[str, ...] = ()
    pending_work_owner: str | SharedOperationPendingWorkOwner | None = None
    capability: SharedOperationCapabilityV1 | SharedOperationCapabilityFactsV1 | Mapping[str, Any] | None = (
        None
    )
    secondary: SharedOperationSecondaryV1 | SharedOperationSecondaryFactsV1 | Mapping[str, Any] | None = None
    artifacts: SharedOperationArtifactsV1 | SharedOperationArtifactsFactsV1 | Mapping[str, Any] | None = None
    effect_owners: tuple[object, ...] = ()
    publishers: tuple[object, ...] = ()


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise SharedOperationViewError(f"{field}_{detail}")


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _state(value: object) -> SharedOperationViewState:
    try:
        return SharedOperationViewState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise SharedOperationViewError("view_closed") from exc


def _reason(value: object) -> SharedOperationViewReason:
    try:
        return SharedOperationViewReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise SharedOperationViewError("reason_closed") from exc


def _owner(value: object) -> SharedOperationPendingWorkOwner:
    if isinstance(value, SharedOperationPendingWorkOwner):
        return value
    if type(value) is not str:
        _fail("pending_work_owner")
    try:
        return SharedOperationPendingWorkOwner(value.strip().casefold())
    except ValueError as exc:
        raise SharedOperationViewError("pending_work_owner_invalid") from exc


def _safe_hostname(value: object) -> str:
    if type(value) is not str or not value or len(value) > MAX_HOSTNAME_CHARS or value != value.strip():
        _fail("source", "invalid")
    if any(unicodedata.category(char).startswith("C") for char in value):
        _fail("source", "invalid")
    if "://" in value or "/" in value or "\\" in value or ":" in value or "@" in value:
        _fail("source", "invalid")
    labels = value.casefold().split(".")
    if not labels or any(_HOST_LABEL_RE.fullmatch(label) is None for label in labels):
        _fail("source", "invalid")
    return value.casefold()


def _sources(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail("sources", "sequence")
    if len(value) > MAX_AUTHORIZED_SOURCE_COUNT:
        _fail("sources", "count")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        source = _safe_hostname(item)
        if source in seen:
            _fail("source", "duplicate")
        seen.add(source)
        result.append(source)
    return tuple(result)


def _claims(value: object, *, field: str) -> int:
    if value is None:
        return 1
    if type(value) is int:
        if not 0 <= value <= MAX_OWNER_CLAIMS:
            _fail(field, "count")
        return cast(int, value)
    if isinstance(value, (str, bytes, bytearray)):
        values: tuple[object, ...] = (value,)
    elif isinstance(value, Sequence):
        values = tuple(value)
    else:
        _fail(field, "sequence")
    if len(values) > MAX_OWNER_CLAIMS:
        _fail(field, "count")
    for item in values:
        if type(item) is not str or not item or item != item.strip():
            _fail(field, "invalid")
        if any(unicodedata.category(char).startswith("C") for char in item):
            _fail(field, "invalid")
        if "://" in item or "/" in item or "\\" in item:
            _fail(field, "private")
    return len(values)


def _progress(value: object) -> OperationProgressProjection:
    if isinstance(value, OperationProgressProjection):
        return value
    if isinstance(value, Mapping):
        return build_operation_progress(value)
    _fail("progress", "invalid")


def _component(
    value: object,
    *,
    kind: str,
    identity: str,
    turn_id: str,
) -> object:
    builder: Any
    schema: str
    cls: type[Any]
    facts_cls: type[Any]
    if kind == "binding":
        builder = build_shared_operation_binding
        schema = SHARED_OPERATION_BINDING_SCHEMA
        cls = SharedOperationBindingV1
        facts_cls = SharedOperationBindingFactsV1
    elif kind == "capability":
        builder = build_shared_operation_capability
        schema = SHARED_OPERATION_CAPABILITY_SCHEMA
        cls = SharedOperationCapabilityV1
        facts_cls = SharedOperationCapabilityFactsV1
    elif kind == "secondary":
        builder = build_shared_operation_secondary
        schema = SHARED_OPERATION_SECONDARY_SCHEMA
        cls = SharedOperationSecondaryV1
        facts_cls = SharedOperationSecondaryFactsV1
    else:
        builder = build_shared_operation_artifacts
        schema = SHARED_OPERATION_ARTIFACTS_SCHEMA
        cls = SharedOperationArtifactsV1
        facts_cls = SharedOperationArtifactsFactsV1
    if value is None:
        return builder(identity, turn_id)
    if isinstance(value, cls):
        value.__post_init__()
        return value
    if isinstance(value, facts_cls):
        return builder(identity, turn_id, facts=value)
    if isinstance(value, Mapping):
        if value.get("schema") == schema and any(key in value for key in ("reason", "state", kind)):
            raw = dict(value)
            raw.setdefault("authenticated_turn_id", turn_id)
            raw.setdefault(
                {
                    "binding": "binding_id",
                    "capability": "capability_id",
                    "secondary": "secondary_id",
                    "artifacts": "artifacts_id",
                }[kind],
                identity,
            )
            return builder(raw)
        return builder(identity, turn_id, facts=value)
    return builder(identity, turn_id, facts={"invalid": value})


def _component_as(value: object, cls: type[object]) -> object:
    if not isinstance(value, cls):
        _fail("component", "invalid")
    return value


@dataclass(frozen=True, slots=True)
class SharedOperationViewV1:
    """One immutable cross-organ operational situation."""

    view_id: str
    authenticated_turn_id: str
    view: SharedOperationViewState
    operation_id: str | None
    mode: OperationMode | None
    binding: SharedOperationBindingV1
    binding_digest: str | None
    authorized_source_summary: tuple[str, ...]
    ordered_plan: tuple[OperationStep, ...]
    active_step_id: str | None
    pending_work_owner: SharedOperationPendingWorkOwner | None
    inherited_deadline_remaining_sec: int | None
    capability: SharedOperationCapabilityV1
    secondary: SharedOperationSecondaryV1
    artifacts: SharedOperationArtifactsV1
    terminal: bool | None
    terminal_evidence_class: str | None
    effect_owner_count: int
    publisher_count: int
    reason: SharedOperationViewReason
    operation_progress: OperationProgressProjection | None = None

    def __post_init__(self) -> None:
        _identifier(self.view_id, field="view_id")
        _identifier(self.authenticated_turn_id, field="authenticated_turn_id")
        state = _state(self.view)
        reason = _reason(self.reason)
        object.__setattr__(self, "view", state)
        object.__setattr__(self, "reason", reason)
        if self.operation_id is not None:
            _identifier(self.operation_id, field="operation_id")
        if self.mode is not None and not isinstance(self.mode, OperationMode):
            try:
                object.__setattr__(self, "mode", OperationMode(cast(str, self.mode)))
            except (TypeError, ValueError) as exc:
                raise SharedOperationViewError("mode_invalid") from exc
        if not isinstance(self.binding, SharedOperationBindingV1):
            _fail("binding", "type")
        if not isinstance(self.capability, SharedOperationCapabilityV1):
            _fail("capability", "type")
        if not isinstance(self.secondary, SharedOperationSecondaryV1):
            _fail("secondary", "type")
        if not isinstance(self.artifacts, SharedOperationArtifactsV1):
            _fail("artifacts", "type")
        for component in (self.binding, self.capability, self.secondary, self.artifacts):
            component.__post_init__()
        if state is SharedOperationViewState.PROJECTED:
            if self.operation_id is None or self.mode is None or self.operation_progress is None:
                _fail("projected", "missing_facts")
            if self.binding_digest != self.binding.binding_digest:
                _fail("binding_digest", "mismatch")
            if self.operation_progress.operation_id != self.operation_id:
                _fail("operation_id", "mismatch")
            if self.operation_progress.authenticated_turn_id != self.authenticated_turn_id:
                _fail("turn_id", "mismatch")
            if self.operation_progress.mode is not self.mode:
                _fail("mode", "mismatch")
            if self.effect_owner_count > 1 or self.publisher_count > 1:
                _fail("ownership", "multiple")
        elif (
            self.operation_id is not None
            or self.mode is not None
            or self.binding_digest is not None
            or self.authorized_source_summary
            or self.ordered_plan
            or self.active_step_id is not None
            or self.pending_work_owner is not None
            or self.inherited_deadline_remaining_sec is not None
            or self.terminal is not None
            or self.terminal_evidence_class is not None
            or self.effect_owner_count
            or self.publisher_count
            or self.operation_progress is not None
        ):
            _fail("non_projected", "exposes_facts")

    @property
    def state(self) -> SharedOperationViewState:
        return self.view

    @property
    def view_state(self) -> SharedOperationViewState:
        return self.view

    @property
    def projection(self) -> SharedOperationViewState:
        return self.view

    @property
    def closed_view(self) -> SharedOperationViewState:
        return self.view

    @property
    def decision(self) -> SharedOperationViewState:
        return self.view

    @property
    def closed_reason(self) -> SharedOperationViewReason:
        return self.reason

    @property
    def ordered_steps(self) -> tuple[OperationStep, ...]:
        return self.ordered_plan

    @property
    def plan(self) -> tuple[OperationStep, ...]:
        return self.ordered_plan

    @property
    def active_step(self) -> OperationStep | None:
        if self.active_step_id is None:
            return None
        return next((step for step in self.ordered_plan if step.step_id == self.active_step_id), None)

    @property
    def deadline_remaining_sec(self) -> int | None:
        return self.inherited_deadline_remaining_sec

    @property
    def deadline(self) -> int | None:
        return self.inherited_deadline_remaining_sec

    @property
    def authorized_sources(self) -> tuple[str, ...]:
        return self.authorized_source_summary

    @property
    def capability_availability(self) -> SharedOperationCapabilityState:
        return self.capability.capability

    @property
    def secondary_availability(self) -> SharedOperationSecondaryState:
        return self.secondary.secondary

    @property
    def artifact_summary(self) -> SharedOperationArtifactsV1:
        return self.artifacts

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": SHARED_OPERATION_VIEW_SCHEMA,
            "view_id": self.view_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "view": self.view.value,
            "operation_id": self.operation_id,
            "mode": self.mode.value if self.mode is not None else None,
            "binding": self.binding.to_mapping(),
            "binding_digest": self.binding_digest,
            "authorized_source_summary": list(self.authorized_source_summary),
            "ordered_plan": [step.to_mapping() for step in self.ordered_plan],
            "active_step_id": self.active_step_id,
            "pending_work_owner": self.pending_work_owner.value
            if self.pending_work_owner is not None
            else None,
            "inherited_deadline_remaining_sec": self.inherited_deadline_remaining_sec,
            "capability": self.capability.to_mapping(),
            "secondary": self.secondary.to_mapping(),
            "artifacts": self.artifacts.to_mapping(),
            "terminal": self.terminal,
            "terminal_evidence_class": self.terminal_evidence_class,
            "effect_owner_count": self.effect_owner_count,
            "publisher_count": self.publisher_count,
            "reason": self.reason.value,
            "operation_progress": self.operation_progress.to_mapping()
            if self.operation_progress is not None
            else None,
        }


OperationViewState = SharedOperationViewState
OperationViewReason = SharedOperationViewReason
PendingWorkOwner = SharedOperationPendingWorkOwner
SharedOperationView = SharedOperationViewV1
SharedOperationFacts = SharedOperationViewFactsV1


def _empty_component(
    identity: str, turn_id: str
) -> tuple[
    SharedOperationBindingV1,
    SharedOperationCapabilityV1,
    SharedOperationSecondaryV1,
    SharedOperationArtifactsV1,
]:
    return (
        cast(SharedOperationBindingV1, build_shared_operation_binding(identity, turn_id)),
        cast(SharedOperationCapabilityV1, build_shared_operation_capability(identity, turn_id)),
        cast(SharedOperationSecondaryV1, build_shared_operation_secondary(identity, turn_id)),
        cast(SharedOperationArtifactsV1, build_shared_operation_artifacts(identity, turn_id)),
    )


def _blocked_component(kind: str, identity: str, turn_id: str) -> object:
    if kind == "binding":
        return build_shared_operation_binding(identity, turn_id, facts={"invalid": True})
    if kind == "capability":
        return build_shared_operation_capability(identity, turn_id, facts={"invalid": True})
    if kind == "secondary":
        return build_shared_operation_secondary(identity, turn_id, facts={"invalid": True})
    return build_shared_operation_artifacts(identity, turn_id, facts={"invalid": True})


def _result(
    view_id: str,
    turn_id: str,
    state: SharedOperationViewState,
    reason: SharedOperationViewReason,
    *,
    operation_id: str | None = None,
    mode: OperationMode | None = None,
    binding: SharedOperationBindingV1 | None = None,
    sources: tuple[str, ...] = (),
    ordered_plan: tuple[OperationStep, ...] = (),
    active_step_id: str | None = None,
    pending_owner: SharedOperationPendingWorkOwner | None = None,
    deadline: int | None = None,
    capability: SharedOperationCapabilityV1 | None = None,
    secondary: SharedOperationSecondaryV1 | None = None,
    artifacts: SharedOperationArtifactsV1 | None = None,
    terminal: bool | None = None,
    terminal_evidence_class: str | None = None,
    effect_owner_count: int = 0,
    publisher_count: int = 0,
    operation_progress: OperationProgressProjection | None = None,
) -> SharedOperationViewV1:
    if state is not SharedOperationViewState.PROJECTED:
        empty_binding, empty_capability, empty_secondary, empty_artifacts = _empty_component(view_id, turn_id)
        binding, capability, secondary, artifacts = (
            empty_binding,
            empty_capability,
            empty_secondary,
            empty_artifacts,
        )
        operation_id = None
        mode = None
        sources = ()
        ordered_plan = ()
        active_step_id = None
        pending_owner = None
        deadline = None
        terminal = None
        terminal_evidence_class = None
        effect_owner_count = 0
        publisher_count = 0
        operation_progress = None
        binding_digest = None
    else:
        if binding is None or capability is None or secondary is None or artifacts is None:
            raise SharedOperationViewError("projected_components_missing")
        binding_digest = binding.binding_digest
    return SharedOperationViewV1(
        view_id=view_id,
        authenticated_turn_id=turn_id,
        view=state,
        operation_id=operation_id,
        mode=mode,
        binding=cast(SharedOperationBindingV1, binding),
        binding_digest=binding_digest,
        authorized_source_summary=sources,
        ordered_plan=ordered_plan,
        active_step_id=active_step_id,
        pending_work_owner=pending_owner,
        inherited_deadline_remaining_sec=deadline,
        capability=cast(SharedOperationCapabilityV1, capability),
        secondary=cast(SharedOperationSecondaryV1, secondary),
        artifacts=cast(SharedOperationArtifactsV1, artifacts),
        terminal=terminal,
        terminal_evidence_class=terminal_evidence_class,
        effect_owner_count=effect_owner_count,
        publisher_count=publisher_count,
        reason=reason,
        operation_progress=operation_progress,
    )


def _mapping_facts(raw: Mapping[str, Any]) -> tuple[object, ...]:
    allowed = {
        "schema",
        "view_id",
        "authenticated_turn_id",
        "facts",
        "operation_progress",
        "progress",
        "binding",
        "binding_facts",
        "authorized_source_summary",
        "authorized_sources",
        "sources",
        "source_summary",
        "pending_work_owner",
        "pending_owner",
        "capability",
        "capability_facts",
        "secondary",
        "secondary_facts",
        "artifacts",
        "artifact_facts",
        "effect_owners",
        "effect_owner_count",
        "publishers",
        "publisher_count",
        "view",
        "state",
        "operation_id",
        "mode",
        "ordered_plan",
        "ordered_steps",
        "active_step_id",
        "inherited_deadline_remaining_sec",
        "deadline_remaining_sec",
        "terminal",
        "terminal_evidence_class",
        "binding_digest",
        "reason",
    }
    if set(raw) - allowed:
        _fail("view", "unknown_fields")
    return (
        raw.get("operation_progress", raw.get("progress")),
        raw.get("binding", raw.get("binding_facts")),
        raw.get(
            "authorized_source_summary",
            raw.get("authorized_sources", raw.get("sources", raw.get("source_summary", ()))),
        ),
        raw.get("pending_work_owner", raw.get("pending_owner", _MISSING)),
        raw.get("capability", raw.get("capability_facts")),
        raw.get("secondary", raw.get("secondary_facts")),
        raw.get("artifacts", raw.get("artifact_facts")),
        raw.get("effect_owners", raw.get("effect_owner_count", None)),
        raw.get("publishers", raw.get("publisher_count", None)),
    )


def build_shared_operation_view(
    view_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    operation_progress: OperationProgressProjection | Mapping[str, Any] | None = None,
    *,
    facts: SharedOperationViewFactsV1 | Mapping[str, Any] | None = None,
    progress: OperationProgressProjection | Mapping[str, Any] | None = None,
    binding: object = None,
    authorized_source_summary: object = None,
    authorized_sources: object = None,
    pending_work_owner: object = _MISSING,
    pending_owner: object = _MISSING,
    capability: object = None,
    secondary: object = None,
    artifacts: object = None,
    effect_owners: object = None,
    effect_owner_count: object = None,
    publishers: object = None,
    publisher_count: object = None,
) -> SharedOperationViewV1:
    """Compose a view from immutable supplied facts only."""

    if isinstance(view_id, Mapping):
        raw = view_id
        try:
            allowed_output = {"view", "state", "reason"}
            if raw.get("schema", SHARED_OPERATION_VIEW_SCHEMA) != SHARED_OPERATION_VIEW_SCHEMA:
                _fail("schema")
            view_key = _identifier(raw.get("view_id"), field="view_id")
            turn_key = _identifier(raw.get("authenticated_turn_id"), field="authenticated_turn_id")
            if allowed_output.intersection(raw):
                try:
                    output_state = _state(raw.get("view", raw.get("state")))
                    output_reason = _reason(raw.get("reason"))
                    if output_state is not SharedOperationViewState.PROJECTED:
                        return _result(view_key, turn_key, output_state, output_reason)
                    progress_raw = raw.get("operation_progress")
                    progress_value = _progress(progress_raw)
                    binding_value = _component(
                        raw.get("binding"), kind="binding", identity=view_key, turn_id=turn_key
                    )
                    capability_value = _component(
                        raw.get("capability"), kind="capability", identity=view_key, turn_id=turn_key
                    )
                    secondary_value = _component(
                        raw.get("secondary"), kind="secondary", identity=view_key, turn_id=turn_key
                    )
                    artifacts_value = _component(
                        raw.get("artifacts"), kind="artifacts", identity=view_key, turn_id=turn_key
                    )
                    if any(
                        getattr(getattr(component, "state", None), "value", None) == "blocked"
                        for component in (binding_value, capability_value, secondary_value, artifacts_value)
                    ):
                        return _result(
                            view_key,
                            turn_key,
                            SharedOperationViewState.BLOCKED,
                            SharedOperationViewReason.COMPONENT_BLOCKED,
                        )
                    return _compose(
                        view_key,
                        turn_key,
                        progress_value,
                        cast(SharedOperationBindingV1, binding_value),
                        cast(SharedOperationCapabilityV1, capability_value),
                        cast(SharedOperationSecondaryV1, secondary_value),
                        cast(SharedOperationArtifactsV1, artifacts_value),
                        raw.get("authorized_source_summary", ()),
                        raw.get("pending_work_owner", _MISSING),
                        raw.get("effect_owner_count", None),
                        raw.get("publisher_count", None),
                    )
                except (TypeError, ValueError, OperationProgressError):
                    return _result(
                        view_key,
                        turn_key,
                        SharedOperationViewState.BLOCKED,
                        SharedOperationViewReason.INVALID_FACTS,
                    )
            values = _mapping_facts(raw)
            view_id = view_key
            authenticated_turn_id = turn_key
            operation_progress = cast(OperationProgressProjection | Mapping[str, Any] | None, values[0])
            binding, sources, pending, capability, secondary, artifacts, owners, pubs = values[1:]
            if "facts" in raw:
                facts = cast(SharedOperationViewFactsV1 | Mapping[str, Any], raw["facts"])
            else:
                facts = SharedOperationViewFactsV1(
                    operation_progress=cast(
                        OperationProgressProjection | Mapping[str, Any] | None, operation_progress
                    ),
                    binding=cast(Any, binding),
                    authorized_source_summary=cast(tuple[str, ...], sources or ()),
                    pending_work_owner=None if pending is _MISSING else cast(Any, pending),
                    capability=cast(Any, capability),
                    secondary=cast(Any, secondary),
                    artifacts=cast(Any, artifacts),
                    effect_owners=tuple(owners)
                    if isinstance(owners, Sequence) and not isinstance(owners, (str, bytes, bytearray))
                    else (() if owners is None else (owners,)),
                    publishers=tuple(pubs)
                    if isinstance(pubs, Sequence) and not isinstance(pubs, (str, bytes, bytearray))
                    else (() if pubs is None else (pubs,)),
                )
            operation_progress = None
        except (TypeError, ValueError):
            view_id = cast(str, raw.get("view_id", "view"))
            authenticated_turn_id = cast(str, raw.get("authenticated_turn_id", "turn"))
            view_key = _identifier(view_id, field="view_id")
            turn_key = _identifier(authenticated_turn_id, field="authenticated_turn_id")
            return _result(
                view_key, turn_key, SharedOperationViewState.BLOCKED, SharedOperationViewReason.INVALID_FACTS
            )

    view_key = _identifier(view_id, field="view_id")
    turn_key = _identifier(authenticated_turn_id, field="authenticated_turn_id")
    if facts is not None:
        try:
            if any(
                item is not None and item is not _MISSING
                for item in (
                    operation_progress,
                    progress,
                    binding,
                    authorized_source_summary,
                    authorized_sources,
                    pending_work_owner if pending_work_owner is not _MISSING else None,
                    pending_owner if pending_owner is not _MISSING else None,
                    capability,
                    secondary,
                    artifacts,
                    effect_owners,
                    effect_owner_count,
                    publishers,
                    publisher_count,
                )
            ):
                _fail("facts", "duplicate_arguments")
            if isinstance(facts, SharedOperationViewFactsV1):
                operation_progress = facts.operation_progress
                binding = facts.binding
                authorized_source_summary = facts.authorized_source_summary
                pending_work_owner = facts.pending_work_owner
                capability = facts.capability
                secondary = facts.secondary
                artifacts = facts.artifacts
                effect_owners = facts.effect_owners
                publishers = facts.publishers
            elif isinstance(facts, Mapping):
                values = _mapping_facts(facts)
                operation_progress = cast(OperationProgressProjection | Mapping[str, Any] | None, values[0])
                (
                    binding,
                    sources,
                    pending,
                    capability,
                    secondary,
                    artifacts,
                    effect_owners,
                    publishers,
                ) = values[1:]
                authorized_source_summary = sources
                pending_work_owner = None if pending is _MISSING else pending
            else:
                _fail("facts", "type")
        except SharedOperationViewError:
            return _result(
                view_key, turn_key, SharedOperationViewState.BLOCKED, SharedOperationViewReason.INVALID_FACTS
            )
    if progress is not None:
        if operation_progress is not None:
            return _result(
                view_key, turn_key, SharedOperationViewState.BLOCKED, SharedOperationViewReason.INVALID_FACTS
            )
        operation_progress = progress
    if operation_progress is None:
        has_supplemental = any(
            item is not None and item is not _MISSING
            for item in (
                binding,
                authorized_source_summary,
                authorized_sources,
                pending_work_owner if pending_work_owner is not _MISSING else None,
                pending_owner if pending_owner is not _MISSING else None,
                capability,
                secondary,
                artifacts,
                effect_owners,
                effect_owner_count,
                publishers,
                publisher_count,
            )
        )
        if not has_supplemental:
            return _result(
                view_key, turn_key, SharedOperationViewState.EMPTY, SharedOperationViewReason.NO_FACTS
            )
        return _result(
            view_key, turn_key, SharedOperationViewState.BLOCKED, SharedOperationViewReason.PROGRESS_INVALID
        )
    try:
        progress_value = _progress(operation_progress)
    except (TypeError, ValueError, OperationProgressError):
        return _result(
            view_key, turn_key, SharedOperationViewState.BLOCKED, SharedOperationViewReason.PROGRESS_INVALID
        )
    try:
        binding_value = cast(
            SharedOperationBindingV1, _component(binding, kind="binding", identity=view_key, turn_id=turn_key)
        )
        capability_value = cast(
            SharedOperationCapabilityV1,
            _component(capability, kind="capability", identity=view_key, turn_id=turn_key),
        )
        secondary_value = cast(
            SharedOperationSecondaryV1,
            _component(secondary, kind="secondary", identity=view_key, turn_id=turn_key),
        )
        artifacts_value = cast(
            SharedOperationArtifactsV1,
            _component(artifacts, kind="artifacts", identity=view_key, turn_id=turn_key),
        )
    except (TypeError, ValueError):
        return _result(
            view_key, turn_key, SharedOperationViewState.BLOCKED, SharedOperationViewReason.COMPONENT_BLOCKED
        )
    return _compose(
        view_key,
        turn_key,
        progress_value,
        binding_value,
        capability_value,
        secondary_value,
        artifacts_value,
        authorized_source_summary if authorized_source_summary is not None else authorized_sources,
        pending_work_owner if pending_work_owner is not _MISSING else pending_owner,
        effect_owner_count if effect_owner_count is not None else effect_owners,
        publisher_count if publisher_count is not None else publishers,
    )


def _compose(
    view_id: str,
    turn_id: str,
    progress: OperationProgressProjection,
    binding: SharedOperationBindingV1,
    capability: SharedOperationCapabilityV1,
    secondary: SharedOperationSecondaryV1,
    artifacts: SharedOperationArtifactsV1,
    sources: object,
    pending_owner: object,
    effect_owners: object,
    publishers: object,
) -> SharedOperationViewV1:
    components = (binding, capability, secondary, artifacts)
    if any(component.state.value == "blocked" for component in components):
        return _result(
            view_id, turn_id, SharedOperationViewState.BLOCKED, SharedOperationViewReason.COMPONENT_BLOCKED
        )
    if any(component.authenticated_turn_id != turn_id for component in components):
        return _result(
            view_id, turn_id, SharedOperationViewState.BLOCKED, SharedOperationViewReason.IDENTITY_MISMATCH
        )
    try:
        source_values = _sources(sources)
        owner_value = (
            SharedOperationPendingWorkOwner.PRIMARY
            if pending_owner is None or pending_owner is _MISSING
            else _owner(pending_owner)
        )
        effect_count = _claims(effect_owners, field="effect_owners")
        publisher_count = _claims(publishers, field="publishers")
    except SharedOperationViewError as exc:
        code = str(exc)
        reason = (
            SharedOperationViewReason.SOURCE_INVALID
            if code.startswith("source") or code.startswith("sources")
            else SharedOperationViewReason.PENDING_OWNER_INVALID
            if code.startswith("pending_work_owner")
            else SharedOperationViewReason.INVALID_FACTS
        )
        return _result(view_id, turn_id, SharedOperationViewState.BLOCKED, reason)
    if effect_count > 1:
        return _result(
            view_id,
            turn_id,
            SharedOperationViewState.BLOCKED,
            SharedOperationViewReason.MULTIPLE_EFFECT_OWNERS,
        )
    if publisher_count > 1:
        return _result(
            view_id, turn_id, SharedOperationViewState.BLOCKED, SharedOperationViewReason.MULTIPLE_PUBLISHERS
        )
    if (
        secondary.secondary is SharedOperationSecondaryState.ABSENT
        and owner_value is SharedOperationPendingWorkOwner.SECONDARY
    ):
        return _result(
            view_id, turn_id, SharedOperationViewState.BLOCKED, SharedOperationViewReason.SECONDARY_OWNERSHIP
        )
    evidence_class = artifacts.terminal_evidence_class
    return _result(
        view_id,
        turn_id,
        SharedOperationViewState.PROJECTED,
        SharedOperationViewReason.PROJECTED,
        operation_id=progress.operation_id,
        mode=progress.mode,
        binding=binding,
        sources=source_values,
        ordered_plan=progress.ordered_steps,
        active_step_id=progress.active_step_id,
        pending_owner=owner_value,
        deadline=progress.hard_deadline_remaining_sec,
        capability=capability,
        secondary=secondary,
        artifacts=artifacts,
        terminal=progress.terminal,
        terminal_evidence_class=evidence_class,
        effect_owner_count=effect_count,
        publisher_count=publisher_count,
        operation_progress=progress,
    )


def validate_shared_operation_view(value: object) -> bool:
    try:
        if isinstance(value, SharedOperationViewV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping) or value.get("schema") != SHARED_OPERATION_VIEW_SCHEMA:
            return False
        result = build_shared_operation_view(value)
        return result.view is not SharedOperationViewState.BLOCKED
    except (TypeError, ValueError):
        return False


build_operation_view = build_shared_operation_view
validate_operation_view = validate_shared_operation_view


__all__ = [
    "SHARED_OPERATION_VIEW_SCHEMA",
    "OperationViewReason",
    "OperationViewState",
    "PendingWorkOwner",
    "SharedOperationFacts",
    "SharedOperationPendingWorkOwner",
    "SharedOperationView",
    "SharedOperationViewError",
    "SharedOperationViewFactsV1",
    "SharedOperationViewReason",
    "SharedOperationViewState",
    "SharedOperationViewV1",
    "build_operation_view",
    "build_shared_operation_view",
    "validate_operation_view",
    "validate_shared_operation_view",
]
