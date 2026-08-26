"""Immutable product-policy identity for the optional semantic supervisor.

This module is deliberately neutral: orchestration may put the policy in the
trusted prompt envelope while the secondary scheduler independently admits the
one runtime workload.  Neither side needs to import the other.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

SUPERVISOR_POLICY_SCHEMA = "friday.supervisor-policy.v1"
SUPERVISOR_PRODUCT_POLICY_ID = "gptoss20b-semantic-supervisor-v1"
SUPERVISOR_ASSIST_POLICY_SCHEMA = "friday.supervisor-policy.v2"
SUPERVISOR_ASSIST_PRODUCT_POLICY_ID = "gptoss20b-semantic-supervisor-v2"
SUPERVISOR_RUNTIME_PROFILE_ID = "gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f"
SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256 = (
    "93ea5698b8b6a9bf8a7dc697ffe37d7353055aa16555188991747bba73d059e3"
)
SUPERVISOR_WORKLOAD = "plan_candidate"
SUPERVISOR_EFFECTIVE_MODE = "shadow"
SUPERVISOR_REQUESTED_MODES = frozenset({"shadow", "assist", "canary"})
SUPERVISOR_ASSIST_REQUESTED_MODES = frozenset({"assist", "canary"})
SUPERVISOR_ADMITTED_TASKS = frozenset(
    {
        "compare_archive_with_current_web",
        "compare_current_file_with_current_web",
    }
)
SUPERVISOR_ASSIST_ADMITTED_TASKS = frozenset({"compare_current_file_with_current_web"})

# Exact end-to-end ceilings for the first bounded comparison journey.  They are
# product policy, not loose runtime hints: any change creates a new policy hash
# and therefore invalidates previously admitted promotion evidence.
SUPERVISOR_TURN_DEADLINE_MS = 12_000
SUPERVISOR_STEP_DEADLINE_MS = 12_000
SUPERVISOR_MAX_CAPABILITY_CALLS = 2
SUPERVISOR_ASSIST_MAX_CAPABILITY_CALLS = 3
SUPERVISOR_MAX_TOOL_CALLS = 0
SUPERVISOR_PRIMARY_MODEL_CALLS = 2
SUPERVISOR_PLANNING_OUTPUT_TOKENS = 512
SUPERVISOR_PRIMARY_OUTPUT_TOKENS = 1_024
SUPERVISOR_REVIEW_OUTPUT_TOKENS = 256
SUPERVISOR_EFFECT_SHADOW_POLICY_SCHEMA = "friday.supervisor-effect-shadow-policy.v1"
SUPERVISOR_EFFECT_SHADOW_POLICY_ID = "gptoss20b-semantic-supervisor-effect-shadow-v1"
SUPERVISOR_EFFECT_WORKLOAD = "effect_planning"
SUPERVISOR_EFFECT_SHADOW_OUTPUT_TOKENS = 128


def _canonical_sha256(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


_EXPECTED_SUPERVISOR_PRODUCT_POLICY: Mapping[str, object] = MappingProxyType(
    {
        "schema": SUPERVISOR_POLICY_SCHEMA,
        "policy_id": SUPERVISOR_PRODUCT_POLICY_ID,
        "status": "shadow_ready",
        "runtime_profile_id": SUPERVISOR_RUNTIME_PROFILE_ID,
        "runtime_profile_manifest_sha256": SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256,
        "runtime_profile_admission": "accepted",
        "runtime_recertification": False,
        "workload": SUPERVISOR_WORKLOAD,
        "requested_modes": tuple(sorted(SUPERVISOR_REQUESTED_MODES)),
        "effective_mode": SUPERVISOR_EFFECTIVE_MODE,
        "admitted_tasks": tuple(sorted(SUPERVISOR_ADMITTED_TASKS)),
        "private_text_required": True,
        "tools_allowed": False,
        "effects_allowed": False,
        "publication_allowed": False,
        "knowledge_writes_allowed": False,
        "max_steps": 6,
        "max_parallel_reads": 2,
        "turn_deadline_ms": SUPERVISOR_TURN_DEADLINE_MS,
        "per_step_deadline_ms": SUPERVISOR_STEP_DEADLINE_MS,
        "max_supervisor_calls": 1,
        "max_model_calls": 1 + SUPERVISOR_PRIMARY_MODEL_CALLS,
        "max_tool_calls": SUPERVISOR_MAX_TOOL_CALLS,
        "max_capability_calls": SUPERVISOR_MAX_CAPABILITY_CALLS,
        "max_review_rounds": 0,
        "max_recovery_rounds": 0,
        "max_output_tokens": (SUPERVISOR_PLANNING_OUTPUT_TOKENS + SUPERVISOR_PRIMARY_OUTPUT_TOKENS),
        "promotion_admitted": False,
        "primary_fallback_required": True,
    }
)

# MappingProxyType plus tuple/frozenset leaves makes the exported policy deeply
# immutable.  Keep a separate expected object so monkeypatching the public name
# fails the scheduler admission closed without affecting other workloads.
SUPERVISOR_PRODUCT_POLICY: Mapping[str, object] = MappingProxyType(dict(_EXPECTED_SUPERVISOR_PRODUCT_POLICY))
SUPERVISOR_PRODUCT_POLICY_SHA256 = _canonical_sha256(dict(_EXPECTED_SUPERVISOR_PRODUCT_POLICY))

_EXPECTED_SUPERVISOR_ASSIST_PRODUCT_POLICY: Mapping[str, object] = MappingProxyType(
    {
        "schema": SUPERVISOR_ASSIST_POLICY_SCHEMA,
        "policy_id": SUPERVISOR_ASSIST_PRODUCT_POLICY_ID,
        "status": "assist_ready",
        "runtime_profile_id": SUPERVISOR_RUNTIME_PROFILE_ID,
        "runtime_profile_manifest_sha256": SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256,
        "runtime_profile_admission": "accepted",
        "runtime_recertification": False,
        "workload": SUPERVISOR_WORKLOAD,
        "requested_modes": tuple(sorted(SUPERVISOR_ASSIST_REQUESTED_MODES)),
        "effective_mode": SUPERVISOR_EFFECTIVE_MODE,
        "admitted_tasks": tuple(sorted(SUPERVISOR_ASSIST_ADMITTED_TASKS)),
        "private_text_required": True,
        "tools_allowed": False,
        "effects_allowed": False,
        "publication_allowed": False,
        "knowledge_writes_allowed": False,
        "max_steps": 6,
        "max_parallel_reads": 2,
        "turn_deadline_ms": SUPERVISOR_TURN_DEADLINE_MS,
        "per_step_deadline_ms": SUPERVISOR_STEP_DEADLINE_MS,
        "max_supervisor_calls": 2,
        "max_model_calls": 2 + SUPERVISOR_PRIMARY_MODEL_CALLS,
        "max_tool_calls": SUPERVISOR_MAX_TOOL_CALLS,
        "max_capability_calls": SUPERVISOR_ASSIST_MAX_CAPABILITY_CALLS,
        "max_review_rounds": 1,
        "max_recovery_rounds": 1,
        "max_output_tokens": (
            SUPERVISOR_PLANNING_OUTPUT_TOKENS
            + SUPERVISOR_PRIMARY_OUTPUT_TOKENS
            + SUPERVISOR_REVIEW_OUTPUT_TOKENS
        ),
        "promotion_admitted": False,
        "primary_fallback_required": True,
    }
)
SUPERVISOR_ASSIST_PRODUCT_POLICY: Mapping[str, object] = MappingProxyType(
    dict(_EXPECTED_SUPERVISOR_ASSIST_PRODUCT_POLICY)
)
SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256 = _canonical_sha256(dict(_EXPECTED_SUPERVISOR_ASSIST_PRODUCT_POLICY))

_EXPECTED_SUPERVISOR_EFFECT_SHADOW_POLICY: Mapping[str, object] = MappingProxyType(
    {
        "schema": SUPERVISOR_EFFECT_SHADOW_POLICY_SCHEMA,
        "policy_id": SUPERVISOR_EFFECT_SHADOW_POLICY_ID,
        "status": "maturity_gated_shadow",
        "runtime_profile_id": SUPERVISOR_RUNTIME_PROFILE_ID,
        "runtime_profile_manifest_sha256": SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256,
        "runtime_profile_admission": "accepted",
        "runtime_recertification": False,
        "workload": SUPERVISOR_EFFECT_WORKLOAD,
        "requested_modes": ("shadow",),
        "effective_mode": "shadow",
        # A secret-free semantic projection can still contain ordinary private
        # user prose.  It therefore uses only the explicitly private-capable
        # admitted profile; lexical hygiene is not a declassification step.
        "contains_private_text": True,
        "priority": "background",
        "effect_class": "none",
        "tools_allowed": False,
        "effects_allowed": False,
        "publication_allowed": False,
        "knowledge_writes_allowed": False,
        "max_model_calls": 1,
        "max_output_tokens": SUPERVISOR_EFFECT_SHADOW_OUTPUT_TOKENS,
        "maturity_witness_required": True,
        "primary_result_unchanged": True,
    }
)
SUPERVISOR_EFFECT_SHADOW_POLICY: Mapping[str, object] = MappingProxyType(
    dict(_EXPECTED_SUPERVISOR_EFFECT_SHADOW_POLICY)
)
SUPERVISOR_EFFECT_SHADOW_POLICY_SHA256 = _canonical_sha256(dict(_EXPECTED_SUPERVISOR_EFFECT_SHADOW_POLICY))


class SupervisorPolicyClosedReason(StrEnum):
    """Finite scheduler reasons; never retain malformed configuration text."""

    ADMITTED = "admitted"
    MODE_OFF = "mode_off"
    INVALID_MODE = "invalid_mode"
    POLICY_INVALID = "policy_invalid"
    TASK_ALLOWLIST_EMPTY = "task_allowlist_empty"
    TASK_ALLOWLIST_INVALID = "task_allowlist_invalid"
    INVALID_BOUNDS = "invalid_bounds"
    PRIVATE_TEXT_REQUIRED = "private_text_required"
    SECONDARY_DISABLED = "secondary_disabled"
    ACCEPTED_PROFILE_REQUIRED = "accepted_profile_required"
    RUNTIME_PROFILE_MISMATCH = "runtime_profile_mismatch"
    SECONDARY_MISCONFIGURED = "secondary_misconfigured"


@dataclass(frozen=True, slots=True)
class SupervisorPolicyAdmission:
    """One closed decision for the scheduler's PLAN_CANDIDATE overlay."""

    requested_mode: str
    effective_mode: str
    policy_id: str
    policy_sha256: str
    admitted_tasks: frozenset[str]
    workload_available: bool
    closed_reason: SupervisorPolicyClosedReason


@dataclass(frozen=True, slots=True)
class SupervisorEffectShadowPolicyAdmission:
    """Independent scheduler admission for the P5 advisory-only workload."""

    requested_mode: str
    effective_mode: str
    policy_id: str
    policy_sha256: str
    workload_available: bool
    closed_reason: SupervisorPolicyClosedReason


@dataclass(frozen=True, slots=True)
class SupervisorProductPolicyIdentity:
    """Closed code-owned identity selected before any supervisor request."""

    schema: str
    policy_id: str
    policy_sha256: str
    effective_mode: str
    admitted_tasks: frozenset[str]
    max_steps: int
    max_parallel_reads: int
    turn_deadline_ms: int
    per_step_deadline_ms: int
    max_supervisor_calls: int
    max_model_calls: int
    max_tool_calls: int
    max_capability_calls: int
    max_review_rounds: int
    max_recovery_rounds: int
    max_output_tokens: int


def supervisor_product_policy_identity_for_mode(
    requested_mode: object,
) -> SupervisorProductPolicyIdentity:
    """Select P1 for shadow/off and P4-v2 for assist/canary.

    The selected policy still has effective mode ``shadow`` and grants no
    execution or publication authority.  Promotion remains a separate gate.
    """

    normalized = str(requested_mode or "").strip().casefold()
    if normalized in SUPERVISOR_ASSIST_REQUESTED_MODES:
        return SupervisorProductPolicyIdentity(
            schema=SUPERVISOR_ASSIST_POLICY_SCHEMA,
            policy_id=SUPERVISOR_ASSIST_PRODUCT_POLICY_ID,
            policy_sha256=SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256,
            effective_mode=SUPERVISOR_EFFECTIVE_MODE,
            admitted_tasks=SUPERVISOR_ASSIST_ADMITTED_TASKS,
            max_steps=6,
            max_parallel_reads=2,
            turn_deadline_ms=SUPERVISOR_TURN_DEADLINE_MS,
            per_step_deadline_ms=SUPERVISOR_STEP_DEADLINE_MS,
            max_supervisor_calls=2,
            max_model_calls=2 + SUPERVISOR_PRIMARY_MODEL_CALLS,
            max_tool_calls=SUPERVISOR_MAX_TOOL_CALLS,
            max_capability_calls=SUPERVISOR_ASSIST_MAX_CAPABILITY_CALLS,
            max_review_rounds=1,
            max_recovery_rounds=1,
            max_output_tokens=(
                SUPERVISOR_PLANNING_OUTPUT_TOKENS
                + SUPERVISOR_PRIMARY_OUTPUT_TOKENS
                + SUPERVISOR_REVIEW_OUTPUT_TOKENS
            ),
        )
    return SupervisorProductPolicyIdentity(
        schema=SUPERVISOR_POLICY_SCHEMA,
        policy_id=SUPERVISOR_PRODUCT_POLICY_ID,
        policy_sha256=SUPERVISOR_PRODUCT_POLICY_SHA256,
        effective_mode=SUPERVISOR_EFFECTIVE_MODE,
        admitted_tasks=SUPERVISOR_ADMITTED_TASKS,
        max_steps=6,
        max_parallel_reads=2,
        turn_deadline_ms=SUPERVISOR_TURN_DEADLINE_MS,
        per_step_deadline_ms=SUPERVISOR_STEP_DEADLINE_MS,
        max_supervisor_calls=1,
        max_model_calls=1 + SUPERVISOR_PRIMARY_MODEL_CALLS,
        max_tool_calls=SUPERVISOR_MAX_TOOL_CALLS,
        max_capability_calls=SUPERVISOR_MAX_CAPABILITY_CALLS,
        max_review_rounds=0,
        max_recovery_rounds=0,
        max_output_tokens=(SUPERVISOR_PLANNING_OUTPUT_TOKENS + SUPERVISOR_PRIMARY_OUTPUT_TOKENS),
    )


def supervisor_product_policy_identity_for_review_rounds(
    max_review_rounds: object,
) -> SupervisorProductPolicyIdentity | None:
    """Resolve only the two code-owned proposal policy budgets."""

    if type(max_review_rounds) is not int or max_review_rounds not in {0, 1}:
        return None
    mode = "shadow" if max_review_rounds == 0 else "assist"
    return supervisor_product_policy_identity_for_mode(mode)


def admitted_supervisor_tasks(raw: object) -> frozenset[str]:
    """Return all configured tasks or fail the entire allowlist closed."""

    if not isinstance(raw, (tuple, list)) or not raw:
        return frozenset()
    admitted: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            return frozenset()
        normalized = item.strip().casefold()
        if not normalized or normalized not in SUPERVISOR_ADMITTED_TASKS or normalized in admitted:
            return frozenset()
        admitted.add(normalized)
    return frozenset(admitted)


def supervisor_product_policy_is_well_formed(
    policy: Mapping[str, object] | None = None,
) -> bool:
    """Verify the exact code-owned policy rather than accepting a lookalike."""

    candidate = SUPERVISOR_PRODUCT_POLICY if policy is None else policy
    try:
        return bool(
            dict(candidate) == dict(_EXPECTED_SUPERVISOR_PRODUCT_POLICY)
            and _canonical_sha256(dict(candidate)) == SUPERVISOR_PRODUCT_POLICY_SHA256
            and candidate.get("runtime_recertification") is False
        )
    except (AttributeError, TypeError, ValueError):
        return False


def supervisor_assist_product_policy_is_well_formed(
    policy: Mapping[str, object] | None = None,
) -> bool:
    """Verify the distinct assist/P4 policy without weakening P1 identity."""

    candidate = SUPERVISOR_ASSIST_PRODUCT_POLICY if policy is None else policy
    try:
        return bool(
            dict(candidate) == dict(_EXPECTED_SUPERVISOR_ASSIST_PRODUCT_POLICY)
            and _canonical_sha256(dict(candidate)) == SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256
            and candidate.get("runtime_recertification") is False
        )
    except (AttributeError, TypeError, ValueError):
        return False


def supervisor_effect_shadow_policy_is_well_formed(
    policy: Mapping[str, object] | None = None,
) -> bool:
    """Verify the separate P5 workload overlay without granting maturity."""

    candidate = SUPERVISOR_EFFECT_SHADOW_POLICY if policy is None else policy
    try:
        return bool(
            dict(candidate) == dict(_EXPECTED_SUPERVISOR_EFFECT_SHADOW_POLICY)
            and _canonical_sha256(dict(candidate)) == SUPERVISOR_EFFECT_SHADOW_POLICY_SHA256
            and candidate.get("runtime_recertification") is False
            and candidate.get("effects_allowed") is False
            and candidate.get("publication_allowed") is False
        )
    except (AttributeError, TypeError, ValueError):
        return False


def evaluate_supervisor_effect_shadow_policy_admission(
    *,
    requested_mode: object,
    secondary_runtime_state: str,
    profile_admission: str,
    runtime_profile_id: str,
    runtime_profile_manifest_sha256: str,
    allow_private_text: object,
) -> SupervisorEffectShadowPolicyAdmission:
    """Admit only the independent, lowest-priority P5 shadow transport lane.

    This decision deliberately does not accept a maturity witness.  The
    production wrapper must independently hold a process-accepted witness
    before it may submit work to this lane.
    """

    raw_mode = str(requested_mode or "").strip().casefold()
    normalized_mode = raw_mode if raw_mode in {"off", "shadow"} else "invalid"
    reason = SupervisorPolicyClosedReason.ADMITTED
    if normalized_mode == "off":
        reason = SupervisorPolicyClosedReason.MODE_OFF
    elif normalized_mode == "invalid":
        reason = SupervisorPolicyClosedReason.INVALID_MODE
    elif not supervisor_effect_shadow_policy_is_well_formed():
        reason = SupervisorPolicyClosedReason.POLICY_INVALID
    elif allow_private_text is not True:
        reason = SupervisorPolicyClosedReason.PRIVATE_TEXT_REQUIRED
    elif secondary_runtime_state == "disabled":
        reason = SupervisorPolicyClosedReason.SECONDARY_DISABLED
    elif profile_admission != "accepted":
        reason = SupervisorPolicyClosedReason.ACCEPTED_PROFILE_REQUIRED
    elif (
        runtime_profile_id != SUPERVISOR_RUNTIME_PROFILE_ID
        or runtime_profile_manifest_sha256 != SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
    ):
        reason = SupervisorPolicyClosedReason.RUNTIME_PROFILE_MISMATCH
    elif secondary_runtime_state != "configured":
        reason = SupervisorPolicyClosedReason.SECONDARY_MISCONFIGURED
    return SupervisorEffectShadowPolicyAdmission(
        requested_mode=normalized_mode,
        effective_mode=("shadow" if reason is SupervisorPolicyClosedReason.ADMITTED else "off"),
        policy_id=SUPERVISOR_EFFECT_SHADOW_POLICY_ID,
        policy_sha256=SUPERVISOR_EFFECT_SHADOW_POLICY_SHA256,
        workload_available=reason is SupervisorPolicyClosedReason.ADMITTED,
        closed_reason=reason,
    )


def disabled_supervisor_effect_shadow_policy_admission(
    *,
    requested_mode: str = "off",
) -> SupervisorEffectShadowPolicyAdmission:
    """Return a constructor-safe closed P5 default."""

    return evaluate_supervisor_effect_shadow_policy_admission(
        requested_mode=requested_mode,
        secondary_runtime_state="disabled",
        profile_admission="",
        runtime_profile_id="",
        runtime_profile_manifest_sha256="",
        allow_private_text=False,
    )


def evaluate_supervisor_policy_admission(
    *,
    requested_mode: object,
    task_allowlist: object,
    max_steps: object,
    max_review_rounds: object,
    timeout_sec: object,
    allow_private_text: object,
    secondary_runtime_state: str,
    profile_admission: str,
    runtime_profile_id: str,
    runtime_profile_manifest_sha256: str,
) -> SupervisorPolicyAdmission:
    """Admit one shadow-only overlay without recertifying the shared runtime."""

    raw_mode = str(requested_mode or "").strip().casefold()
    if raw_mode == "off":
        normalized_mode = "off"
    elif raw_mode in SUPERVISOR_REQUESTED_MODES:
        normalized_mode = raw_mode
    else:
        normalized_mode = "invalid"
    identity = supervisor_product_policy_identity_for_mode(normalized_mode)
    tasks = admitted_supervisor_tasks(task_allowlist)
    selected_policy_is_well_formed = (
        supervisor_assist_product_policy_is_well_formed()
        if normalized_mode in SUPERVISOR_ASSIST_REQUESTED_MODES
        else supervisor_product_policy_is_well_formed()
    )
    bounds_are_valid = bool(
        isinstance(max_steps, int)
        and not isinstance(max_steps, bool)
        and max_steps == identity.max_steps
        and isinstance(max_review_rounds, int)
        and not isinstance(max_review_rounds, bool)
        and max_review_rounds == identity.max_review_rounds
        and isinstance(timeout_sec, (int, float))
        and not isinstance(timeout_sec, bool)
        and math.isfinite(timeout_sec)
        and int(round(float(timeout_sec) * 1_000)) == identity.turn_deadline_ms
    )

    reason = SupervisorPolicyClosedReason.ADMITTED
    if normalized_mode == "off":
        reason = SupervisorPolicyClosedReason.MODE_OFF
    elif normalized_mode == "invalid":
        reason = SupervisorPolicyClosedReason.INVALID_MODE
    elif not selected_policy_is_well_formed:
        reason = SupervisorPolicyClosedReason.POLICY_INVALID
    elif not tasks or not tasks <= identity.admitted_tasks:
        reason = (
            SupervisorPolicyClosedReason.TASK_ALLOWLIST_EMPTY
            if isinstance(task_allowlist, (tuple, list)) and not task_allowlist
            else SupervisorPolicyClosedReason.TASK_ALLOWLIST_INVALID
        )
    elif not bounds_are_valid:
        reason = SupervisorPolicyClosedReason.INVALID_BOUNDS
    elif allow_private_text is not True:
        reason = SupervisorPolicyClosedReason.PRIVATE_TEXT_REQUIRED
    elif secondary_runtime_state == "disabled":
        reason = SupervisorPolicyClosedReason.SECONDARY_DISABLED
    elif profile_admission != "accepted":
        reason = SupervisorPolicyClosedReason.ACCEPTED_PROFILE_REQUIRED
    elif (
        runtime_profile_id != SUPERVISOR_RUNTIME_PROFILE_ID
        or runtime_profile_manifest_sha256 != SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
    ):
        reason = SupervisorPolicyClosedReason.RUNTIME_PROFILE_MISMATCH
    elif secondary_runtime_state != "configured":
        reason = SupervisorPolicyClosedReason.SECONDARY_MISCONFIGURED

    return SupervisorPolicyAdmission(
        requested_mode=normalized_mode,
        effective_mode=(
            SUPERVISOR_EFFECTIVE_MODE if reason is SupervisorPolicyClosedReason.ADMITTED else "off"
        ),
        policy_id=identity.policy_id,
        policy_sha256=identity.policy_sha256,
        admitted_tasks=tasks,
        workload_available=reason is SupervisorPolicyClosedReason.ADMITTED,
        closed_reason=reason,
    )


def disabled_supervisor_policy_admission(
    *,
    requested_mode: str = "off",
) -> SupervisorPolicyAdmission:
    """Return a constructor-safe closed default for manually built schedulers."""

    return evaluate_supervisor_policy_admission(
        requested_mode=requested_mode,
        task_allowlist=(),
        max_steps=6,
        max_review_rounds=supervisor_product_policy_identity_for_mode(requested_mode).max_review_rounds,
        timeout_sec=12.0,
        allow_private_text=False,
        secondary_runtime_state="disabled",
        profile_admission="",
        runtime_profile_id="",
        runtime_profile_manifest_sha256="",
    )
