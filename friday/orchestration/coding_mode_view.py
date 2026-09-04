"""Read-only Coding Mode situation composition.

The view is a projection of supplied contracts.  It owns no effect, process,
path, archive, network, or worker lifecycle and therefore cannot imply that a
live coding worker exists.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from friday.orchestration.coding_inspect_hazards import (
    CodingInspectHazardsReason,
    CodingInspectHazardsState,
    CodingInspectHazardsV1,
)
from friday.orchestration.coding_inspect_report import (
    CodingInspectReportReason,
    CodingInspectReportState,
    CodingInspectReportV1,
    build_coding_inspect_report,
)
from friday.orchestration.coding_mode_carrier import CodingModeCarrierV1, build_coding_mode_carrier
from friday.orchestration.coding_mode_execute_claim import (
    CodingModeExecuteClaimState,
    CodingModeExecuteClaimV1,
    build_coding_mode_execute_claim,
)
from friday.orchestration.coding_mode_intent import (
    CodingModeIntentV1,
    build_coding_mode_intent,
)
from friday.orchestration.coding_mode_plan_gate import (
    CodingModePlanGateV1,
    build_coding_mode_plan_gate,
)
from friday.orchestration.coding_mode_snapshot import CodingModeSnapshotV1, build_coding_mode_snapshot
from friday.orchestration.coding_project_identity import (
    CodingProjectIdentityState,
    CodingProjectIdentityV1,
    build_coding_project_identity,
)
from friday.orchestration.coding_source_inspect import (
    CodingSourceInspectReason,
    CodingSourceInspectState,
    CodingSourceInspectV1,
)
from friday.orchestration.coding_source_tree import build_coding_source_tree
from friday.orchestration.coding_toolchain_hint import (
    CodingToolchainHintReason,
    CodingToolchainHintState,
    CodingToolchainHintV1,
)
from friday.orchestration.coding_worker_admission import (
    CodingWorkerAdmissionState,
    CodingWorkerAdmissionV1,
    build_coding_worker_admission,
)

CODING_MODE_VIEW_SCHEMA = "friday.coding-mode-view.v1"
MAX_VIEW_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class CodingModeViewError(ValueError):
    """A view identity or composed fact is malformed."""


class CodingModeViewState(StrEnum):
    EMPTY = "empty"
    PROJECTED = "projected"
    BLOCKED = "blocked"


class CodingModeViewReason(StrEnum):
    NO_FACTS = "no_facts"
    PROJECTED = "projected"
    COMPONENT_BLOCKED = "component_blocked"
    WORKER_REQUIRED = "worker_required"
    TURN_MISMATCH = "turn_mismatch"
    INVALID_FACTS = "invalid_facts"


@dataclass(frozen=True, slots=True)
class CodingModeViewFactsV1:
    """Already-built Coding Mode components to project."""

    intent: object | None = None
    snapshot: object | None = None
    execute_claim: object | None = None
    plan_gate: object | None = None
    carrier: object | None = None
    inspect_report: object | None = None
    worker_admission: object | None = None
    project_identity: object | None = None


@dataclass(frozen=True, slots=True)
class CodingModeViewV1:
    """Immutable one-turn Coding Mode situation projection."""

    view_id: str
    authenticated_turn_id: str
    state: CodingModeViewState
    intent: CodingModeIntentV1
    snapshot: CodingModeSnapshotV1
    execute_claim: CodingModeExecuteClaimV1
    plan_gate: CodingModePlanGateV1
    carrier: CodingModeCarrierV1
    inspect_report: CodingInspectReportV1
    worker_admission: CodingWorkerAdmissionV1
    project_identity: CodingProjectIdentityV1
    reason: CodingModeViewReason

    def __post_init__(self) -> None:
        _identifier(self.view_id, "view_id", MAX_VIEW_ID_CHARS)
        _identifier(self.authenticated_turn_id, "authenticated_turn_id", MAX_AUTHENTICATED_TURN_ID_CHARS)
        state = _state(self.state)
        reason = _reason(self.reason)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)
        components = (
            self.intent,
            self.snapshot,
            self.execute_claim,
            self.plan_gate,
            self.carrier,
            self.inspect_report,
            self.worker_admission,
            self.project_identity,
        )
        expected_types = (
            CodingModeIntentV1,
            CodingModeSnapshotV1,
            CodingModeExecuteClaimV1,
            CodingModePlanGateV1,
            CodingModeCarrierV1,
            CodingInspectReportV1,
            CodingWorkerAdmissionV1,
            CodingProjectIdentityV1,
        )
        if any(
            not isinstance(component, expected)
            for component, expected in zip(components, expected_types, strict=True)
        ):
            raise CodingModeViewError("component_type_invalid")
        for component in components:
            component.__post_init__()
        if any(component.authenticated_turn_id != self.authenticated_turn_id for component in components):
            raise CodingModeViewError("component_turn_mismatch")
        if state is CodingModeViewState.EMPTY and any(
            getattr(component, "state", getattr(component, "intent", None)).value != "empty"
            for component in components
        ):
            raise CodingModeViewError("empty_view_has_facts")
        if state is CodingModeViewState.PROJECTED:
            if any(
                getattr(component, "state", getattr(component, "intent", None)).value == "blocked"
                for component in components
            ):
                raise CodingModeViewError("projected_view_has_blocked_component")
            if self.execute_claim.claim is CodingModeExecuteClaimState.EXECUTE_CLAIMED and (
                self.worker_admission.admission is not CodingWorkerAdmissionState.ADMITTED
            ):
                raise CodingModeViewError("projected_view_lacks_worker")
        if state is not CodingModeViewState.PROJECTED and reason is CodingModeViewReason.PROJECTED:
            raise CodingModeViewError("non_projected_reason_invalid")

    @property
    def view_state(self) -> CodingModeViewState:
        return self.state

    @property
    def decision(self) -> CodingModeViewState:
        return self.state

    @property
    def projected(self) -> bool:
        return self.state is CodingModeViewState.PROJECTED

    @property
    def worker_admitted(self) -> bool:
        return self.worker_admission.admission is CodingWorkerAdmissionState.ADMITTED

    @property
    def live_process_claimed(self) -> bool:
        return False

    @property
    def closed_reason(self) -> CodingModeViewReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_MODE_VIEW_SCHEMA,
            "view_id": self.view_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "state": self.state.value,
            "intent": self.intent.to_mapping(),
            "snapshot": self.snapshot.to_mapping(),
            "execute_claim": self.execute_claim.to_mapping(),
            "plan_gate": self.plan_gate.to_mapping(),
            "carrier": self.carrier.to_mapping(),
            "inspect_report": self.inspect_report.to_mapping(),
            "worker_admission": self.worker_admission.to_mapping(),
            "project_identity": {
                "schema": "friday.coding-project-identity.v1",
                "identity_id": self.project_identity.identity_id,
                "authenticated_turn_id": self.project_identity.authenticated_turn_id,
                "identity": self.project_identity.identity.value,
                "project_id": self.project_identity.project_id,
                "revision_selector": self.project_identity.revision_selector,
                "reason": self.project_identity.reason.value,
            },
            "reason": self.reason.value,
        }


CodingModeView = CodingModeViewV1
CodingModeViewFacts = CodingModeViewFactsV1
ViewState = CodingModeViewState
ViewReason = CodingModeViewReason


def _identifier(value: object, field: str, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        raise CodingModeViewError(f"{field}_id_invalid")
    return cast(str, value)


def _state(value: object) -> CodingModeViewState:
    try:
        return value if isinstance(value, CodingModeViewState) else CodingModeViewState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingModeViewError("view_closed") from exc


def _reason(value: object) -> CodingModeViewReason:
    try:
        return value if isinstance(value, CodingModeViewReason) else CodingModeViewReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingModeViewError("reason_closed") from exc


def _intent(value: object, view_id: str, turn: str) -> CodingModeIntentV1:
    if isinstance(value, CodingModeIntentV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        if {"intent", "state", "reason"}.intersection(value):
            return build_coding_mode_intent(value)
        return build_coding_mode_intent(f"{view_id}:intent", turn, value)
    return build_coding_mode_intent(f"{view_id}:intent", turn)


def _snapshot(value: object, view_id: str, turn: str) -> CodingModeSnapshotV1:
    if isinstance(value, CodingModeSnapshotV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        if {"snapshot", "state", "reason"}.intersection(value):
            return build_coding_mode_snapshot(value)
        return build_coding_mode_snapshot(f"{view_id}:snapshot", turn, members=value)
    return build_coding_mode_snapshot(f"{view_id}:snapshot", turn)


def _execute(value: object, view_id: str, turn: str) -> CodingModeExecuteClaimV1:
    if isinstance(value, CodingModeExecuteClaimV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        if {"claim", "state", "reason"}.intersection(value):
            return build_coding_mode_execute_claim(value)
        return build_coding_mode_execute_claim(f"{view_id}:execute", turn, facts=value)
    return build_coding_mode_execute_claim(f"{view_id}:execute", turn)


def _gate(value: object, view_id: str, turn: str) -> CodingModePlanGateV1:
    if isinstance(value, CodingModePlanGateV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        if {"gate", "state", "reason"}.intersection(value):
            return build_coding_mode_plan_gate(value)
        return build_coding_mode_plan_gate(f"{view_id}:gate", turn, facts=value)
    return build_coding_mode_plan_gate(f"{view_id}:gate", turn)


def _carrier(value: object, view_id: str, turn: str) -> CodingModeCarrierV1:
    if isinstance(value, CodingModeCarrierV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        if {"carrier", "state", "reason"}.intersection(value):
            return build_coding_mode_carrier(value)
        return build_coding_mode_carrier(f"{view_id}:carrier", turn, facts=value)
    return build_coding_mode_carrier(f"{view_id}:carrier", turn)


def _identity(value: object, view_id: str, turn: str) -> CodingProjectIdentityV1:
    if isinstance(value, CodingProjectIdentityV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        if {"identity", "state", "reason"}.intersection(value):
            from friday.orchestration.coding_project_identity import (
                CodingProjectIdentityReason,
            )

            return CodingProjectIdentityV1(
                cast(str, value.get("identity_id")),
                cast(str, value.get("authenticated_turn_id")),
                cast(CodingProjectIdentityState, value.get("identity", value.get("state"))),
                cast(str | None, value.get("project_id")),
                cast(str | None, value.get("revision_selector")),
                cast(CodingProjectIdentityReason, value.get("reason")),
            )
        return build_coding_project_identity(
            cast(str, value.get("identity_id", f"{view_id}:identity")),
            cast(str, value.get("authenticated_turn_id", turn)),
            value,
        )
    return build_coding_project_identity(f"{view_id}:identity", turn)


def _inspect(value: object, view_id: str, turn: str) -> CodingInspectReportV1:
    if isinstance(value, CodingInspectReportV1):
        value.__post_init__()
        return value
    if not isinstance(value, Mapping):
        return build_coding_inspect_report(f"{view_id}:inspect", turn)
    if {"report", "state", "reason"}.intersection(value):
        try:
            tree_raw = value.get("tree")
            inspection_raw = value.get("inspection")
            hazards_raw = value.get("hazards")
            hint_raw = value.get("toolchain_hint")
            tree = None
            if isinstance(tree_raw, Mapping):
                tree = build_coding_source_tree(
                    cast(str, tree_raw.get("tree_id")),
                    cast(str, tree_raw.get("authenticated_turn_id")),
                    tree_raw.get("members", ()),
                )
            inspection = None
            if isinstance(inspection_raw, Mapping):
                inspection = CodingSourceInspectV1(
                    cast(str, inspection_raw.get("inspect_id")),
                    cast(str, inspection_raw.get("authenticated_turn_id")),
                    cast(
                        CodingSourceInspectState,
                        inspection_raw.get("inspection", inspection_raw.get("state")),
                    ),
                    cast(int, inspection_raw.get("member_count")),
                    cast(int, inspection_raw.get("file_count")),
                    cast(int, inspection_raw.get("directory_count")),
                    cast(int, inspection_raw.get("executable_member_count", 0)),
                    cast(CodingSourceInspectReason, inspection_raw.get("reason")),
                )
            hazards = None
            if isinstance(hazards_raw, Mapping):
                hazards = CodingInspectHazardsV1(
                    cast(str, hazards_raw.get("hazard_id")),
                    cast(str, hazards_raw.get("authenticated_turn_id")),
                    cast(CodingInspectHazardsState, hazards_raw.get("hazards", hazards_raw.get("state"))),
                    cast(int, hazards_raw.get("member_count")),
                    cast(int, hazards_raw.get("hazard_count")),
                    cast(int, hazards_raw.get("secret_name_count")),
                    cast(int, hazards_raw.get("executable_member_count")),
                    cast(int, hazards_raw.get("nested_vcs_dir_count")),
                    tuple(cast(list[str], hazards_raw.get("hazard_kinds", []))),
                    cast(CodingInspectHazardsReason, hazards_raw.get("reason")),
                )
            hint = None
            if isinstance(hint_raw, Mapping):
                hint = CodingToolchainHintV1(
                    cast(str, hint_raw.get("hint_id")),
                    cast(str, hint_raw.get("authenticated_turn_id")),
                    cast(CodingToolchainHintState, hint_raw.get("hint", hint_raw.get("state"))),
                    cast(int, hint_raw.get("member_count")),
                    tuple(cast(list[str], hint_raw.get("detected_suffixes", []))),
                    tuple(cast(list[str], hint_raw.get("language_hints", []))),
                    cast(CodingToolchainHintReason, hint_raw.get("reason")),
                )
            return CodingInspectReportV1(
                cast(str, value.get("report_id")),
                cast(str, value.get("authenticated_turn_id")),
                cast(CodingInspectReportState, value.get("report", value.get("state"))),
                tree,
                inspection,
                hazards,
                hint,
                cast(int, value.get("member_count")),
                cast(CodingInspectReportReason, value.get("reason")),
            )
        except (TypeError, ValueError):
            return build_coding_inspect_report(
                f"{view_id}:inspect", turn, members=({"relative_path": "../blocked"},)
            )
    return build_coding_inspect_report(
        cast(str, value.get("report_id", f"{view_id}:inspect")),
        cast(str, value.get("authenticated_turn_id", turn)),
        value.get("tree", value.get("members", ())),
        inspection=value.get("inspection"),
        hazards=value.get("hazards"),
        toolchain_hint=value.get("toolchain_hint"),
    )


def _worker(value: object, view_id: str, turn: str) -> CodingWorkerAdmissionV1:
    if isinstance(value, CodingWorkerAdmissionV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        return build_coding_worker_admission(value)
    return build_coding_worker_admission(f"{view_id}:worker", turn)


def _empty_components(
    view_id: str, turn: str
) -> tuple[
    CodingModeIntentV1,
    CodingModeSnapshotV1,
    CodingModeExecuteClaimV1,
    CodingModePlanGateV1,
    CodingModeCarrierV1,
    CodingInspectReportV1,
    CodingWorkerAdmissionV1,
    CodingProjectIdentityV1,
]:
    return (
        _intent(None, view_id, turn),
        _snapshot(None, view_id, turn),
        _execute(None, view_id, turn),
        _gate(None, view_id, turn),
        _carrier(None, view_id, turn),
        _inspect(None, view_id, turn),
        _worker(None, view_id, turn),
        _identity(None, view_id, turn),
    )


def _result(
    view_id: str,
    turn: str,
    state: CodingModeViewState,
    components: tuple[
        CodingModeIntentV1,
        CodingModeSnapshotV1,
        CodingModeExecuteClaimV1,
        CodingModePlanGateV1,
        CodingModeCarrierV1,
        CodingInspectReportV1,
        CodingWorkerAdmissionV1,
        CodingProjectIdentityV1,
    ],
    reason: CodingModeViewReason,
) -> CodingModeViewV1:
    return CodingModeViewV1(view_id, turn, state, *components, reason)


def build_coding_mode_view(
    view_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    intent: object = None,
    snapshot: object = None,
    execute_claim: object = None,
    plan_gate: object = None,
    carrier: object = None,
    inspect_report: object = None,
    worker_admission: object = None,
    project_identity: object = None,
    *,
    facts: CodingModeViewFactsV1 | Mapping[str, object] | None = None,
) -> CodingModeViewV1:
    """Compose a Coding Mode view from already-built, body-free contracts."""

    if isinstance(view_id, Mapping):
        raw = view_id
        allowed = {
            "schema",
            "view_id",
            "authenticated_turn_id",
            "state",
            "intent",
            "snapshot",
            "execute_claim",
            "plan_gate",
            "carrier",
            "inspect_report",
            "worker_admission",
            "project_identity",
            "reason",
        }
        if set(raw) - allowed:
            raise CodingModeViewError("view_mapping_unknown_fields")
        if {"state", "reason"}.intersection(raw):
            required = {
                "schema",
                "view_id",
                "authenticated_turn_id",
                "state",
                "intent",
                "snapshot",
                "execute_claim",
                "plan_gate",
                "carrier",
                "inspect_report",
                "worker_admission",
                "project_identity",
                "reason",
            }
            if set(raw) != required or raw.get("schema") != CODING_MODE_VIEW_SCHEMA:
                raise CodingModeViewError("view_mapping_serialized_invalid")
            view_key = _identifier(raw.get("view_id"), "view_id", MAX_VIEW_ID_CHARS)
            turn_key = _identifier(
                raw.get("authenticated_turn_id"),
                "authenticated_turn_id",
                MAX_AUTHENTICATED_TURN_ID_CHARS,
            )
            try:
                components = (
                    _intent(raw.get("intent"), view_key, turn_key),
                    _snapshot(raw.get("snapshot"), view_key, turn_key),
                    _execute(raw.get("execute_claim"), view_key, turn_key),
                    _gate(raw.get("plan_gate"), view_key, turn_key),
                    _carrier(raw.get("carrier"), view_key, turn_key),
                    _inspect(raw.get("inspect_report"), view_key, turn_key),
                    _worker(raw.get("worker_admission"), view_key, turn_key),
                    _identity(raw.get("project_identity"), view_key, turn_key),
                )
                return CodingModeViewV1(
                    view_key,
                    turn_key,
                    cast(CodingModeViewState, raw.get("state")),
                    *components,
                    cast(CodingModeViewReason, raw.get("reason")),
                )
            except (TypeError, ValueError) as exc:
                raise CodingModeViewError("view_mapping_components_invalid") from exc
        view_id = cast(str, raw.get("view_id"))
        authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
        facts = CodingModeViewFactsV1(
            raw.get("intent"),
            raw.get("snapshot"),
            raw.get("execute_claim"),
            raw.get("plan_gate"),
            raw.get("carrier"),
            raw.get("inspect_report"),
            raw.get("worker_admission"),
            raw.get("project_identity"),
        )
    view_key = _identifier(view_id, "view_id", MAX_VIEW_ID_CHARS)
    turn_key = _identifier(authenticated_turn_id, "authenticated_turn_id", MAX_AUTHENTICATED_TURN_ID_CHARS)
    if facts is not None:
        if any(
            item is not None
            for item in (
                intent,
                snapshot,
                execute_claim,
                plan_gate,
                carrier,
                inspect_report,
                worker_admission,
                project_identity,
            )
        ):
            raise CodingModeViewError("facts_and_explicit_components_mixed")
        if isinstance(facts, CodingModeViewFactsV1):
            fact_values = facts
        elif isinstance(facts, Mapping):
            allowed = {
                "intent",
                "snapshot",
                "execute_claim",
                "plan_gate",
                "carrier",
                "inspect_report",
                "worker_admission",
                "project_identity",
            }
            if set(facts) - allowed:
                raise CodingModeViewError("view_facts_unknown_fields")
            fact_keys = (
                "intent",
                "snapshot",
                "execute_claim",
                "plan_gate",
                "carrier",
                "inspect_report",
                "worker_admission",
                "project_identity",
            )
            fact_values = CodingModeViewFactsV1(*(facts.get(key) for key in fact_keys))
        else:
            raise CodingModeViewError("view_facts_invalid")
    else:
        fact_values = CodingModeViewFactsV1(
            intent,
            snapshot,
            execute_claim,
            plan_gate,
            carrier,
            inspect_report,
            worker_admission,
            project_identity,
        )
    raw_components = (
        fact_values.intent,
        fact_values.snapshot,
        fact_values.execute_claim,
        fact_values.plan_gate,
        fact_values.carrier,
        fact_values.inspect_report,
        fact_values.worker_admission,
        fact_values.project_identity,
    )
    if all(item is None for item in raw_components):
        return _result(
            view_key,
            turn_key,
            CodingModeViewState.EMPTY,
            _empty_components(view_key, turn_key),
            CodingModeViewReason.NO_FACTS,
        )
    try:
        components = (
            _intent(fact_values.intent, view_key, turn_key),
            _snapshot(fact_values.snapshot, view_key, turn_key),
            _execute(fact_values.execute_claim, view_key, turn_key),
            _gate(fact_values.plan_gate, view_key, turn_key),
            _carrier(fact_values.carrier, view_key, turn_key),
            _inspect(fact_values.inspect_report, view_key, turn_key),
            _worker(fact_values.worker_admission, view_key, turn_key),
            _identity(fact_values.project_identity, view_key, turn_key),
        )
    except (TypeError, ValueError):
        return _result(
            view_key,
            turn_key,
            CodingModeViewState.BLOCKED,
            _empty_components(view_key, turn_key),
            CodingModeViewReason.INVALID_FACTS,
        )
    if any(component.authenticated_turn_id != turn_key for component in components):
        return _result(
            view_key,
            turn_key,
            CodingModeViewState.BLOCKED,
            _empty_components(view_key, turn_key),
            CodingModeViewReason.TURN_MISMATCH,
        )
    if any(
        getattr(component, "state", getattr(component, "intent", None)).value == "blocked"
        for component in components
    ):
        return _result(
            view_key,
            turn_key,
            CodingModeViewState.BLOCKED,
            components,
            CodingModeViewReason.COMPONENT_BLOCKED,
        )
    execute_value = components[2]
    worker_value = components[6]
    if (
        execute_value.claim is CodingModeExecuteClaimState.EXECUTE_CLAIMED
        and worker_value.admission is not CodingWorkerAdmissionState.ADMITTED
    ):
        return _result(
            view_key, turn_key, CodingModeViewState.BLOCKED, components, CodingModeViewReason.WORKER_REQUIRED
        )
    return _result(
        view_key, turn_key, CodingModeViewState.PROJECTED, components, CodingModeViewReason.PROJECTED
    )


build_mode_view = build_coding_mode_view


__all__ = [
    "CODING_MODE_VIEW_SCHEMA",
    "CodingModeView",
    "CodingModeViewError",
    "CodingModeViewFacts",
    "CodingModeViewFactsV1",
    "CodingModeViewReason",
    "CodingModeViewState",
    "CodingModeViewV1",
    "ViewReason",
    "ViewState",
    "build_coding_mode_view",
    "build_mode_view",
]
