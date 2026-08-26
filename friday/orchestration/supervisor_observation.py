"""Privacy-safe structural observations for supervisor shadow.  No bodies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from friday import semantic_supervisor_policy
from friday.orchestration.supervisor_contracts import (
    SupervisorMode,
    canonical_sha256,
)

SUPERVISOR_OBSERVATION_SCHEMA = "friday.supervisor-shadow-observation.v1"


class SupervisorSkipReason(StrEnum):
    MODE_OFF = "mode_off"
    EXACT_LANE = "exact_lane"
    SMALL_TALK = "small_talk"
    ORDINARY_DIALOGUE = "ordinary_dialogue"
    ESTABLISHED_FILE_READ = "established_file_read"
    TASK_NOT_ALLOWLISTED = "task_not_allowlisted"
    SECONDARY_UNAVAILABLE = "secondary_unavailable"
    SPECIAL_SURFACE = "special_surface"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    SECRET_MATERIAL = "secret_material"
    BINDING_UNAVAILABLE = "binding_unavailable"
    MALFORMED_PROPOSAL = "malformed_proposal"
    POLICY_REJECTED = "policy_rejected"
    WORKLOAD_DISALLOWED = "workload_disallowed"
    SATURATED = "saturated"
    TIMEOUT = "timeout"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class SupervisorObservation:
    supervisor_mode: SupervisorMode
    requested_mode: str
    effective_mode: str
    promotion_admitted: bool
    invoked: bool
    skip_reason: SupervisorSkipReason
    policy_id: str
    policy_sha256: str
    accepted_profile_id: str
    manifest_digest: str
    supervisor_input_digest: str
    proposal_digest: str
    proposal_parse_status: str
    policy_verdict: str
    policy_reason: str
    task_class: str
    step_count: int
    effect_classes: tuple[str, ...]
    fallback_owner: str
    publication_owner: str
    endpoint_health_class: str
    current_route: str
    runtime_owner: str
    planner_latency_bucket: str
    review_latency_bucket: str
    primary_trace_digest: str
    capability_outcome_classes: tuple[str, ...]
    completion_verdict: str
    publication_result: str
    authority_rechecked: str
    state_restored: str
    retry_occurred: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema": SUPERVISOR_OBSERVATION_SCHEMA,
            "supervisor_mode": self.supervisor_mode.value,
            "requested_mode": self.requested_mode,
            "effective_mode": self.effective_mode,
            "promotion_admitted": self.promotion_admitted,
            "invoked": self.invoked,
            "skip_reason": self.skip_reason.value,
            "policy_id": self.policy_id,
            "policy_sha256": self.policy_sha256,
            "accepted_profile_id": self.accepted_profile_id,
            "manifest_digest": self.manifest_digest,
            "supervisor_input_digest": self.supervisor_input_digest,
            "proposal_digest": self.proposal_digest,
            "proposal_parse_status": self.proposal_parse_status,
            "policy_verdict": self.policy_verdict,
            "policy_reason": self.policy_reason,
            "task_class": self.task_class,
            "step_count": self.step_count,
            "effect_classes": list(self.effect_classes),
            "fallback_owner": self.fallback_owner,
            "publication_owner": self.publication_owner,
            "endpoint_health_class": self.endpoint_health_class,
            "current_route": self.current_route,
            "runtime_owner": self.runtime_owner,
            "planner_latency_bucket": self.planner_latency_bucket,
            "review_latency_bucket": self.review_latency_bucket,
            "primary_trace_digest": self.primary_trace_digest,
            "capability_outcome_classes": list(self.capability_outcome_classes),
            "completion_verdict": self.completion_verdict,
            "publication_result": self.publication_result,
            "authority_rechecked": self.authority_rechecked,
            "state_restored": self.state_restored,
            "retry_occurred": self.retry_occurred,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())

    def with_primary_trace(
        self,
        *,
        trace_digest: str,
        capability_outcomes: tuple[str, ...],
        completion: str,
        publication: str,
        authority_rechecked: bool,
        state_restored: bool,
        retry_occurred: bool,
    ) -> SupervisorObservation:
        """Join only closed facts from the already committed primary trace."""

        return replace(
            self,
            primary_trace_digest=trace_digest,
            capability_outcome_classes=capability_outcomes,
            completion_verdict=completion,
            publication_result=publication,
            authority_rechecked="yes" if authority_rechecked else "no",
            state_restored="yes" if state_restored else "no",
            retry_occurred="yes" if retry_occurred else "no",
        )


def skipped_observation(
    *,
    requested_mode: str,
    skip_reason: SupervisorSkipReason,
    current_route: str = "legacy",
    accepted_profile_id: str = "",
    manifest_digest: str = "",
    supervisor_input_digest: str = "",
) -> SupervisorObservation:
    mode = SupervisorMode.fail_closed(requested_mode)
    identity = semantic_supervisor_policy.supervisor_product_policy_identity_for_mode(
        requested_mode
    )
    return SupervisorObservation(
        supervisor_mode=mode,
        requested_mode=requested_mode,
        effective_mode="shadow" if mode is not SupervisorMode.OFF else "off",
        promotion_admitted=False,
        invoked=False,
        skip_reason=skip_reason,
        policy_id=identity.policy_id,
        policy_sha256=identity.policy_sha256,
        accepted_profile_id=accepted_profile_id,
        manifest_digest=manifest_digest,
        supervisor_input_digest=supervisor_input_digest,
        proposal_digest="",
        proposal_parse_status="skipped",
        policy_verdict="not_evaluated",
        policy_reason="none",
        task_class="",
        step_count=0,
        effect_classes=(),
        fallback_owner="primary_only",
        publication_owner="primary",
        endpoint_health_class="not_called",
        current_route=current_route,
        runtime_owner="unchanged",
        planner_latency_bucket="not_called",
        review_latency_bucket="not_called",
        primary_trace_digest="",
        capability_outcome_classes=(),
        completion_verdict="unavailable",
        publication_result="unavailable",
        authority_rechecked="unavailable",
        state_restored="unavailable",
        retry_occurred="unavailable",
    )


def parsed_observation(
    *,
    requested_mode: str,
    manifest_digest: str,
    supervisor_input_digest: str,
    proposal_digest: str,
    proposal_parse_status: str,
    policy_verdict: str,
    policy_reason: str,
    task_class: str,
    step_count: int,
    effect_classes: tuple[str, ...],
    current_route: str,
    endpoint_health_class: str,
    accepted_profile_id: str,
    skip_reason: SupervisorSkipReason = SupervisorSkipReason.NONE,
    invoked: bool = True,
    planner_latency_bucket: str = "unavailable",
) -> SupervisorObservation:
    mode = SupervisorMode.fail_closed(requested_mode)
    identity = semantic_supervisor_policy.supervisor_product_policy_identity_for_mode(
        requested_mode
    )
    return SupervisorObservation(
        supervisor_mode=mode,
        requested_mode=requested_mode,
        effective_mode="shadow",
        promotion_admitted=False,
        invoked=invoked,
        skip_reason=skip_reason,
        policy_id=identity.policy_id,
        policy_sha256=identity.policy_sha256,
        accepted_profile_id=accepted_profile_id,
        manifest_digest=manifest_digest,
        supervisor_input_digest=supervisor_input_digest,
        proposal_digest=proposal_digest,
        proposal_parse_status=proposal_parse_status,
        policy_verdict=policy_verdict,
        policy_reason=policy_reason,
        task_class=task_class,
        step_count=step_count,
        effect_classes=effect_classes,
        fallback_owner="primary_only",
        publication_owner="primary",
        endpoint_health_class=endpoint_health_class,
        current_route=current_route,
        runtime_owner="unchanged",
        planner_latency_bucket=planner_latency_bucket,
        review_latency_bucket="not_called",
        primary_trace_digest="",
        capability_outcome_classes=(),
        completion_verdict="unavailable",
        publication_result="unavailable",
        authority_rechecked="unavailable",
        state_restored="unavailable",
        retry_occurred="unavailable",
    )


def observation_contains_forbidden_body(observation: Mapping[str, Any], bodies: tuple[str, ...]) -> bool:
    serialized = canonical_sha256(dict(observation)) + str(sorted(observation.items()))
    return any(body and body in serialized for body in bodies)
