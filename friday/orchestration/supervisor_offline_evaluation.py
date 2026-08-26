"""Deterministic synthetic runtime replay for the P1 supervisor surface.

The harness drives :class:`SemanticSupervisorShadowRuntime` with an in-memory
primary and an in-memory model adapter.  It installs no endpoint and exposes no
executable capability.  The resulting body-free report is source regression
evidence only, never live shadow, canary, promotion, or acceptance evidence.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from friday import semantic_supervisor_policy
from friday.orchestration.contracts import TurnInput
from friday.orchestration.semantic_supervisor_runtime import SemanticSupervisorShadowRuntime
from friday.orchestration.supervisor_contracts import (
    HOST_SCAN_LOCAL_ID,
    TaskClass,
    canonical_dumps,
    canonical_sha256,
)
from friday.orchestration.supervisor_observation import SupervisorObservation, SupervisorSkipReason
from friday.permissions import ActorContext
from friday.secondary_brain import (
    ModelRequest,
    SecondaryAttempt,
    SecondaryFailure,
    SecondaryResult,
)

OFFLINE_FIXTURE_SET_SCHEMA = "friday.semantic-supervisor-offline-fixtures.v1"
OFFLINE_EVALUATION_SCHEMA = "friday.semantic-supervisor-offline-evaluation.v1"
OFFLINE_EVIDENCE_KIND = "synthetic_offline_fixture_replay"

_CASE_ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ATTACHMENT_KINDS = frozenset({"none", "text_available", "text_unavailable"})
_EXPECTED_LANES = frozenset({"exact_lane", "primary_only", "supervisor_candidate"})
_PROPOSAL_CASES = frozenset({"none", "valid", "stale_manifest", "unknown_capability", "malformed"})
_EXPECTED_POLICY_OUTCOMES = frozenset(
    {"not_evaluated", "admitted", "stale_manifest", "unknown_capability", "malformed"}
)
_ALLOWED_TASKS = frozenset(
    {
        TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB.value,
        TaskClass.COMPARE_ARCHIVE_WITH_CURRENT_WEB.value,
    }
)

_OFFLINE_ACTOR_ID = "offline_synthetic_actor"
_OFFLINE_CONVERSATION_ID = "offline_synthetic_conversation"


class OfflineEvaluationError(ValueError):
    """The synthetic fixture set is outside the closed offline contract."""


def _closed_keys(value: Mapping[str, Any], expected: frozenset[str], *, label: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise OfflineEvaluationError(f"{label} keys are not closed (missing={missing}, extra={extra})")


def _required_bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise OfflineEvaluationError(f"{label} must be a boolean")
    return value


def _required_enum(value: object, allowed: frozenset[str], *, label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise OfflineEvaluationError(f"{label} is outside the closed enum")
    return value


@dataclass(frozen=True, slots=True)
class _FixtureSettings:
    mode: str
    tasks: tuple[str, ...]
    max_steps: int

    def payload(self) -> dict[str, Any]:
        return {"mode": self.mode, "tasks": list(self.tasks), "max_steps": self.max_steps}

    def runtime(self) -> SimpleNamespace:
        return SimpleNamespace(
            semantic_supervisor_mode=self.mode,
            semantic_supervisor_tasks=self.tasks,
            semantic_supervisor_max_steps=self.max_steps,
            semantic_supervisor_max_review_rounds=0,
            semantic_supervisor_timeout_sec=12.0,
            secondary_llm_profile=semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
        )


@dataclass(frozen=True, slots=True)
class _OfflineFixture:
    case_id: str
    message: str
    conversation_present: bool
    enable_tools: bool
    attachment: str
    pending_bound: bool
    proposal_case: str
    expected_lane: str
    expected_skip_reason: str
    expected_policy_outcome: str

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "turn": {
                "message": self.message,
                "conversation_present": self.conversation_present,
                "enable_tools": self.enable_tools,
                "attachment": self.attachment,
            },
            "pending_bound": self.pending_bound,
            "proposal_case": self.proposal_case,
            "expected": {
                "lane": self.expected_lane,
                "skip_reason": self.expected_skip_reason,
                "policy_outcome": self.expected_policy_outcome,
            },
        }

    def attachments(self) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        if self.attachment == "text_available":
            attachments.append({"mime_type": "text/plain", "text": "synthetic fixture text"})
        elif self.attachment == "text_unavailable":
            attachments.append({"mime_type": "application/pdf"})
        return attachments

    def turn_input(self) -> TurnInput:
        return TurnInput.from_chat(
            message=self.message,
            actor=SimpleNamespace(is_owner=True, shared_tenant=False),
            conversation_id=_OFFLINE_CONVERSATION_ID if self.conversation_present else None,
            attachments=self.attachments(),
            enable_tools=self.enable_tools,
            synthetic_document_notice=False,
            mode="dialogue",
            reply_to=None,
            quoted_attachment_reference=False,
            reply_assistant_reference=False,
        )


@dataclass(frozen=True, slots=True)
class _FixtureSet:
    settings: _FixtureSettings
    fixtures: tuple[_OfflineFixture, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "schema": OFFLINE_FIXTURE_SET_SCHEMA,
            "evidence_kind": OFFLINE_EVIDENCE_KIND,
            "settings": self.settings.payload(),
            "fixtures": [item.payload() for item in self.fixtures],
        }


def _parse_settings(value: object) -> _FixtureSettings:
    if not isinstance(value, Mapping):
        raise OfflineEvaluationError("settings must be an object")
    item = dict(value)
    _closed_keys(item, frozenset({"mode", "tasks", "max_steps"}), label="settings")
    if item["mode"] != "shadow":
        raise OfflineEvaluationError("offline fixture mode must be shadow")
    tasks_raw = item["tasks"]
    if not isinstance(tasks_raw, list) or not tasks_raw:
        raise OfflineEvaluationError("settings.tasks must be a non-empty list")
    tasks = tuple(tasks_raw)
    if any(not isinstance(task, str) or task not in _ALLOWED_TASKS for task in tasks):
        raise OfflineEvaluationError("settings.tasks contains an unadmitted task")
    if len(set(tasks)) != len(tasks):
        raise OfflineEvaluationError("settings.tasks must be unique")
    max_steps = item["max_steps"]
    if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps != 6:
        raise OfflineEvaluationError("settings.max_steps must equal the admitted P1 bound 6")
    return _FixtureSettings(mode="shadow", tasks=tasks, max_steps=max_steps)


def _parse_fixture(value: object) -> _OfflineFixture:
    if not isinstance(value, Mapping):
        raise OfflineEvaluationError("fixture must be an object")
    item = dict(value)
    _closed_keys(
        item,
        frozenset(
            {
                "id",
                "turn",
                "pending_bound",
                "proposal_case",
                "expected",
            }
        ),
        label="fixture",
    )
    case_id = item["id"]
    if not isinstance(case_id, str) or _CASE_ID.fullmatch(case_id) is None:
        raise OfflineEvaluationError("fixture.id has an invalid shape")

    turn_raw = item["turn"]
    if not isinstance(turn_raw, Mapping):
        raise OfflineEvaluationError("fixture.turn must be an object")
    turn = dict(turn_raw)
    _closed_keys(
        turn,
        frozenset({"message", "conversation_present", "enable_tools", "attachment"}),
        label=f"fixture {case_id} turn",
    )
    message = turn["message"]
    if not isinstance(message, str) or not message.strip() or len(message) > 400:
        raise OfflineEvaluationError(f"fixture {case_id} message must contain 1 to 400 characters")

    expected_raw = item["expected"]
    if not isinstance(expected_raw, Mapping):
        raise OfflineEvaluationError(f"fixture {case_id} expected must be an object")
    expected = dict(expected_raw)
    _closed_keys(
        expected,
        frozenset({"lane", "skip_reason", "policy_outcome"}),
        label=f"fixture {case_id} expected",
    )
    skip_reasons = frozenset(reason.value for reason in SupervisorSkipReason)
    proposal_case = _required_enum(
        item["proposal_case"], _PROPOSAL_CASES, label=f"fixture {case_id} proposal_case"
    )
    expected_lane = _required_enum(
        expected["lane"], _EXPECTED_LANES, label=f"fixture {case_id} expected.lane"
    )
    if (expected_lane == "supervisor_candidate") != (proposal_case != "none"):
        raise OfflineEvaluationError(
            f"fixture {case_id} proposal_case must be present only for a supervisor candidate"
        )
    return _OfflineFixture(
        case_id=case_id,
        message=message,
        conversation_present=_required_bool(
            turn["conversation_present"], label=f"fixture {case_id} conversation_present"
        ),
        enable_tools=_required_bool(turn["enable_tools"], label=f"fixture {case_id} enable_tools"),
        attachment=_required_enum(
            turn["attachment"], _ATTACHMENT_KINDS, label=f"fixture {case_id} attachment"
        ),
        pending_bound=_required_bool(item["pending_bound"], label=f"fixture {case_id} pending_bound"),
        proposal_case=proposal_case,
        expected_lane=expected_lane,
        expected_skip_reason=_required_enum(
            expected["skip_reason"], skip_reasons, label=f"fixture {case_id} expected.skip_reason"
        ),
        expected_policy_outcome=_required_enum(
            expected["policy_outcome"],
            _EXPECTED_POLICY_OUTCOMES,
            label=f"fixture {case_id} expected.policy_outcome",
        ),
    )


def _parse_fixture_set(value: object) -> _FixtureSet:
    if not isinstance(value, Mapping):
        raise OfflineEvaluationError("fixture set must be an object")
    item = dict(value)
    _closed_keys(
        item,
        frozenset({"schema", "evidence_kind", "settings", "fixtures"}),
        label="fixture set",
    )
    if item["schema"] != OFFLINE_FIXTURE_SET_SCHEMA:
        raise OfflineEvaluationError(f"fixture set schema must be {OFFLINE_FIXTURE_SET_SCHEMA}")
    if item["evidence_kind"] != OFFLINE_EVIDENCE_KIND:
        raise OfflineEvaluationError(f"evidence_kind must be {OFFLINE_EVIDENCE_KIND}")
    fixtures_raw = item["fixtures"]
    if not isinstance(fixtures_raw, list) or not 1 <= len(fixtures_raw) <= 64:
        raise OfflineEvaluationError("fixtures must contain 1 to 64 cases")
    fixtures = tuple(_parse_fixture(entry) for entry in fixtures_raw)
    ids = [fixture.case_id for fixture in fixtures]
    if len(set(ids)) != len(ids):
        raise OfflineEvaluationError("fixture ids must be unique")
    return _FixtureSet(settings=_parse_settings(item["settings"]), fixtures=fixtures)


@dataclass(slots=True)
class _ActivityLedger:
    events: list[str]
    execution_count: int = 0
    publication_count: int = 0
    effect_count: int = 0


class _ForbiddenCapabilitySurface:
    """Tripwire proving that runtime replay never reaches an executable seam."""

    def __init__(self, ledger: _ActivityLedger) -> None:
        self._ledger = ledger

    def __getattr__(self, name: str) -> Any:
        self._ledger.execution_count += 1
        self._ledger.effect_count += 1
        self._ledger.events.append("forbidden_capability_surface_access")
        raise OfflineEvaluationError(f"offline runtime accessed forbidden capability surface: {name}")


class _SyntheticPrimary:
    mode = "legacy"

    def __init__(
        self,
        *,
        ledger: _ActivityLedger,
        pending_bound: bool,
        conversation_id: str | None,
    ) -> None:
        self._ledger = ledger
        self._pending_bound = pending_bound
        self.calls = 0
        self.close_calls = 0
        self.last_kg: object = None
        self.last_hybrid_searcher: object = None
        self.marker = {
            "conversation_id": conversation_id,
            "response_class": "synthetic_primary",
            "stable": {"unchanged": True},
        }

    def pending_durable_turn_admission(self, *_args: Any, **_kwargs: Any) -> bool:
        return self._pending_bound

    async def chat(self, user_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        del user_id, message
        self.calls += 1
        self.last_kg = kwargs.get("kg")
        self.last_hybrid_searcher = kwargs.get("hybrid_searcher")
        self._ledger.events.append("primary_enter")
        self._ledger.events.append("primary_return")
        return self.marker

    async def close(self) -> None:
        self.close_calls += 1


class _SyntheticScheduler:
    """In-memory proposal source; there is deliberately no HTTP client here."""

    def __init__(self, *, proposal_case: str, ledger: _ActivityLedger) -> None:
        self._proposal_case = proposal_case
        self._ledger = ledger
        self.calls = 0
        self.dispatch_count = 0

    @staticmethod
    def _request_template(request: ModelRequest) -> dict[str, Any]:
        try:
            content = request.messages[1]["content"]
            if type(content) is not str:
                raise TypeError("offline request content must be text")
            envelope = json.loads(content)
            template = envelope["untrusted_payload"]["response_template"]
            if type(template) is not dict:
                raise TypeError("offline request template must be an object")
            return deepcopy(template)
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise OfflineEvaluationError("runtime emitted an invalid offline request envelope") from error

    def _proposal(self, request: ModelRequest) -> dict[str, Any]:
        template = self._request_template(request)
        if self._proposal_case == "valid":
            return template
        if self._proposal_case == "stale_manifest":
            template["manifest_id"] = "sha256:" + "0" * 64
            return template
        if self._proposal_case == "unknown_capability":
            capability_steps = [step for step in template["steps"] if step["kind"] == "capability"]
            capability_steps[-1]["target_id"] = HOST_SCAN_LOCAL_ID
            capability_steps[-1]["input"] = {}
            return template
        # A fixture that expects no invocation also returns an invalid object if
        # a regression invokes it, ensuring that no fixture claim can bless it.
        return {"schema": template.get("schema"), "manifest_id": template.get("manifest_id")}

    async def evaluate_shadow(
        self,
        request: ModelRequest,
        *,
        validator: Any = None,
        invalidate_on_rejection: bool = True,
        pre_dispatch_validator: Any = None,
        dispatch_observer: Any = None,
    ) -> SecondaryAttempt:
        if invalidate_on_rejection is not False:
            raise OfflineEvaluationError("runtime changed the non-owning rejection scope")
        self.calls += 1
        if pre_dispatch_validator is not None and pre_dispatch_validator() is not True:
            return SecondaryAttempt.rejected(SecondaryFailure.CANCELLED)
        self.dispatch_count += 1
        self._ledger.events.append("shadow_model_dispatch")
        if dispatch_observer is not None:
            dispatch_observer()
        proposal = self._proposal(request)
        result = SecondaryResult(
            visible_content=canonical_dumps(proposal),
            structured_output=proposal,
            served_model_alias="offline_synthetic_no_endpoint",
        )
        admitted = bool(validator(result)) if validator is not None else False
        if admitted:
            return SecondaryAttempt.success(result)
        return SecondaryAttempt.rejected(SecondaryFailure.MALFORMED_RESPONSE)

    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        self._ledger.execution_count += 1
        self._ledger.events.append("forbidden_execution")
        raise OfflineEvaluationError("offline shadow attempted execution")

    def publish(self, *_args: Any, **_kwargs: Any) -> None:
        self._ledger.publication_count += 1
        self._ledger.events.append("forbidden_publication")
        raise OfflineEvaluationError("offline shadow attempted publication")

    def apply_effect(self, *_args: Any, **_kwargs: Any) -> None:
        self._ledger.effect_count += 1
        self._ledger.events.append("forbidden_effect")
        raise OfflineEvaluationError("offline shadow attempted an effect")


@dataclass(frozen=True, slots=True)
class _RuntimeReplay:
    observation: SupervisorObservation
    status: Mapping[str, object]
    primary_call_count: int
    shadow_call_count: int
    shadow_dispatch_count: int
    primary_response_identity_unchanged: bool
    primary_response_value_unchanged: bool
    shadow_started_after_primary: bool
    opaque_surfaces_forwarded_unchanged: bool
    lifecycle_owner_unchanged: bool
    execution_count: int
    publication_count: int
    effect_count: int


async def _replay_runtime_fixture(
    fixture: _OfflineFixture,
    runtime_settings: object,
) -> _RuntimeReplay:
    ledger = _ActivityLedger(events=[])
    conversation_id = _OFFLINE_CONVERSATION_ID if fixture.conversation_present else None
    primary = _SyntheticPrimary(
        ledger=ledger,
        pending_bound=fixture.pending_bound,
        conversation_id=conversation_id,
    )
    scheduler = _SyntheticScheduler(proposal_case=fixture.proposal_case, ledger=ledger)
    runtime = SemanticSupervisorShadowRuntime(
        settings=runtime_settings,
        primary=primary,
        scheduler=scheduler,
    )
    actor = ActorContext(
        user_id=_OFFLINE_ACTOR_ID,
        preset_key="owner",
        source="offline-synthetic-runtime",
    )
    attachments = fixture.attachments()
    forbidden_surface = _ForbiddenCapabilitySurface(ledger)
    expected_primary_value = deepcopy(primary.marker)
    primary_result = await runtime.chat(
        _OFFLINE_ACTOR_ID,
        fixture.message,
        actor=actor,
        conversation_id=conversation_id,
        attachments=attachments,
        enable_tools=fixture.enable_tools,
        kg=forbidden_surface,
        hybrid_searcher=forbidden_surface,
        _semantic_supervisor_explicit_mode_requested=False,
    )
    if primary_result is not primary.marker:
        ledger.publication_count += 1
        ledger.events.append("supervisor_replaced_primary_response")
    await runtime.drain_shadow()
    observations = runtime.semantic_supervisor_observations
    if len(observations) != 1:
        raise OfflineEvaluationError("offline runtime must emit exactly one observation per fixture")
    status = runtime.semantic_supervisor_status()
    primary_return_index = ledger.events.index("primary_return")
    shadow_started_after_primary = "shadow_model_dispatch" not in ledger.events or (
        ledger.events.index("shadow_model_dispatch") > primary_return_index
    )
    opaque_surfaces_forwarded_unchanged = bool(
        primary.last_kg is forbidden_surface and primary.last_hybrid_searcher is forbidden_surface
    )
    await runtime.close()
    return _RuntimeReplay(
        observation=observations[0],
        status=status,
        primary_call_count=primary.calls,
        shadow_call_count=scheduler.calls,
        shadow_dispatch_count=scheduler.dispatch_count,
        primary_response_identity_unchanged=primary_result is primary.marker,
        primary_response_value_unchanged=primary_result == expected_primary_value,
        shadow_started_after_primary=shadow_started_after_primary,
        opaque_surfaces_forwarded_unchanged=opaque_surfaces_forwarded_unchanged,
        lifecycle_owner_unchanged=primary.close_calls == 0,
        execution_count=ledger.execution_count,
        publication_count=ledger.publication_count,
        effect_count=ledger.effect_count,
    )


def _metric(count: int, denominator_count: int, denominator: str) -> dict[str, Any]:
    rate = round(count / denominator_count, 6) if denominator_count else 0.0
    return {
        "count": count,
        "denominator": denominator,
        "denominator_count": denominator_count,
        "rate": rate,
    }


async def _replay_runtime_fixtures(
    fixture_set: _FixtureSet,
    runtime_settings: object,
) -> tuple[_RuntimeReplay, ...]:
    replays: list[_RuntimeReplay] = []
    for fixture in fixture_set.fixtures:
        replays.append(await _replay_runtime_fixture(fixture, runtime_settings))
    return tuple(replays)


def evaluate_offline_fixture_set(value: object) -> dict[str, Any]:
    """Drive the real shadow wrapper without installing network or capability adapters."""

    fixture_set = _parse_fixture_set(value)
    runtime_settings = fixture_set.settings.runtime()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise OfflineEvaluationError("offline evaluator requires a synchronous caller")
    replays = asyncio.run(_replay_runtime_fixtures(fixture_set, runtime_settings))

    case_reports: list[dict[str, Any]] = []
    valid_proposals = 0
    parsed_proposals = 0
    policy_rejections = 0
    stale_rejections = 0
    unknown_rejections = 0
    unnecessary_invocations = 0
    expected_exact_lanes = 0
    exact_lane_bypasses = 0
    primary_fallback_parity = 0
    fixture_conformance = 0
    runtime_invariant_conformance = 0
    total_primary_calls = 0
    total_shadow_calls = 0
    total_shadow_dispatches = 0
    total_execution_count = 0
    total_publication_count = 0
    total_effect_count = 0

    for fixture, replay in zip(fixture_set.fixtures, replays, strict=True):
        turn = fixture.turn_input()
        observation = replay.observation
        invoked = observation.invoked
        lane = (
            "supervisor_candidate"
            if invoked
            else (
                "exact_lane" if observation.skip_reason is SupervisorSkipReason.EXACT_LANE else "primary_only"
            )
        )
        if invoked and fixture.expected_lane != "supervisor_candidate":
            unnecessary_invocations += 1
        if fixture.expected_lane == "exact_lane":
            expected_exact_lanes += 1
            if not invoked and observation.skip_reason is SupervisorSkipReason.EXACT_LANE:
                exact_lane_bypasses += 1

        parse_status = observation.proposal_parse_status
        policy_verdict = observation.policy_verdict
        policy_reason = observation.policy_reason
        policy_outcome = (
            "malformed"
            if parse_status == "malformed"
            else policy_reason
            if policy_reason != "none"
            else "not_evaluated"
        )
        if parse_status == "parsed":
            parsed_proposals += 1
        if policy_verdict == "valid":
            valid_proposals += 1
        elif policy_verdict == "rejected":
            policy_rejections += 1
            if policy_reason == "stale_manifest":
                stale_rejections += 1
            elif policy_reason == "unknown_capability":
                unknown_rejections += 1

        fallback_parity = bool(
            replay.primary_call_count == 1
            and replay.primary_response_identity_unchanged
            and replay.primary_response_value_unchanged
            and replay.shadow_started_after_primary
            and observation.fallback_owner == "primary_only"
            and observation.publication_owner == "primary"
        )
        if fallback_parity:
            primary_fallback_parity += 1

        status_non_owning = bool(
            replay.status.get("role") == "discarded_advisory_shadow"
            and replay.status.get("promotion_admitted") is False
            and replay.status.get("runtime_owner") == "unchanged"
            and replay.status.get("publication_owner") == "primary"
            and replay.status.get("tools_allowed") is False
            and replay.status.get("effects_allowed") is False
            and replay.status.get("execution_allowed") is False
            and replay.status.get("pending") == 0
        )
        non_owning_conforms = bool(
            status_non_owning
            and replay.lifecycle_owner_unchanged
            and replay.opaque_surfaces_forwarded_unchanged
            and replay.execution_count == 0
            and replay.publication_count == 0
            and replay.effect_count == 0
        )
        runtime_conforms = bool(
            replay.shadow_call_count == int(invoked)
            and replay.shadow_dispatch_count == int(invoked)
            and fallback_parity
            and non_owning_conforms
        )
        if runtime_conforms:
            runtime_invariant_conformance += 1
        conforms = (
            lane == fixture.expected_lane
            and observation.skip_reason.value == fixture.expected_skip_reason
            and policy_outcome == fixture.expected_policy_outcome
            and runtime_conforms
        )
        if conforms:
            fixture_conformance += 1

        total_primary_calls += replay.primary_call_count
        total_shadow_calls += replay.shadow_call_count
        total_shadow_dispatches += replay.shadow_dispatch_count
        total_execution_count += replay.execution_count
        total_publication_count += replay.publication_count
        total_effect_count += replay.effect_count
        case_reports.append(
            {
                "case_id": fixture.case_id,
                "case_digest": canonical_sha256(fixture.payload()),
                "turn_digest": canonical_sha256(turn.model_payload()),
                "lane": lane,
                "invoked": invoked,
                "skip_reason": observation.skip_reason.value,
                "task_class": observation.task_class,
                "manifest_digest_present": bool(observation.manifest_digest),
                "supervisor_input_binding_present": bool(observation.supervisor_input_digest),
                "proposal_binding_present": bool(observation.proposal_digest),
                "proposal_parse_status": parse_status,
                "policy_verdict": policy_verdict,
                "policy_reason": policy_reason,
                "primary_call_count": replay.primary_call_count,
                "shadow_call_count": replay.shadow_call_count,
                "shadow_dispatch_count": replay.shadow_dispatch_count,
                "primary_response_identity_unchanged": replay.primary_response_identity_unchanged,
                "primary_response_value_unchanged": replay.primary_response_value_unchanged,
                "shadow_started_after_primary": replay.shadow_started_after_primary,
                "opaque_surfaces_forwarded_unchanged": replay.opaque_surfaces_forwarded_unchanged,
                "lifecycle_owner_unchanged": replay.lifecycle_owner_unchanged,
                "runtime_status_non_owning": status_non_owning,
                "observed_execution_count": replay.execution_count,
                "observed_publication_count": replay.publication_count,
                "observed_effect_count": replay.effect_count,
                "non_owning_conforms": non_owning_conforms,
                "primary_fallback_parity": fallback_parity,
                "runtime_invariant_conforms": runtime_conforms,
                "fixture_conforms": conforms,
            }
        )

    fixture_count = len(fixture_set.fixtures)
    invocation_count = sum(1 for case in case_reports if case["invoked"] is True)
    stale_or_unknown = stale_rejections + unknown_rejections
    report: dict[str, Any] = {
        "schema": OFFLINE_EVALUATION_SCHEMA,
        "evidence": {
            "kind": OFFLINE_EVIDENCE_KIND,
            "network_used": False,
            "live_shadow_evidence": False,
            "live_canary_evidence": False,
            "promotion_evidence": False,
            "acceptance_authority": "none",
            "warning": "synthetic_offline_only_not_live_shadow_or_canary_acceptance",
        },
        "runtime_harness": {
            "runtime_exercised": True,
            "fixture_primary_trace_used": False,
            "in_memory_model_adapter": True,
            "network_endpoint_installed": False,
            "primary_call_count": total_primary_calls,
            "shadow_call_count": total_shadow_calls,
            "shadow_dispatch_count": total_shadow_dispatches,
            "runtime_invariant_conformance": _metric(
                runtime_invariant_conformance,
                fixture_count,
                "fixtures",
            ),
        },
        "fixture_set": {
            "schema": OFFLINE_FIXTURE_SET_SCHEMA,
            "digest": canonical_sha256(fixture_set.payload()),
            "case_count": fixture_count,
        },
        "metrics": {
            "valid_proposals": _metric(valid_proposals, invocation_count, "invocations"),
            "policy_rejections": _metric(policy_rejections, parsed_proposals, "parsed_proposals"),
            "stale_manifest_rejections": _metric(stale_rejections, parsed_proposals, "parsed_proposals"),
            "unknown_capability_rejections": _metric(
                unknown_rejections, parsed_proposals, "parsed_proposals"
            ),
            "stale_or_unknown_capability_rejections": _metric(
                stale_or_unknown, parsed_proposals, "parsed_proposals"
            ),
            "unnecessary_invocations": _metric(unnecessary_invocations, invocation_count, "invocations"),
            "exact_lane_bypasses": _metric(
                exact_lane_bypasses, expected_exact_lanes, "expected_exact_lane_cases"
            ),
            "primary_fallback_parity": _metric(primary_fallback_parity, fixture_count, "fixtures"),
            "fixture_conformance": _metric(fixture_conformance, fixture_count, "fixtures"),
        },
        "non_owning_counts": {
            "execution_count": total_execution_count,
            "publication_count": total_publication_count,
            "effect_count": total_effect_count,
        },
        "cases": case_reports,
    }
    report["report_sha256"] = canonical_sha256(report)
    return report
