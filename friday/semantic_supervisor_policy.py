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
SUPERVISOR_RUNTIME_PROFILE_ID = "gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f"
SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256 = (
    "93ea5698b8b6a9bf8a7dc697ffe37d7353055aa16555188991747bba73d059e3"
)
SUPERVISOR_WORKLOAD = "plan_candidate"
SUPERVISOR_EFFECTIVE_MODE = "shadow"
SUPERVISOR_REQUESTED_MODES = frozenset({"shadow", "assist", "canary"})
SUPERVISOR_ADMITTED_TASKS = frozenset(
    {
        "compare_archive_with_current_web",
        "compare_current_file_with_current_web",
    }
)


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
        "max_review_rounds": 0,
        "promotion_admitted": False,
        "primary_fallback_required": True,
    }
)

# MappingProxyType plus tuple/frozenset leaves makes the exported policy deeply
# immutable.  Keep a separate expected object so monkeypatching the public name
# fails the scheduler admission closed without affecting other workloads.
SUPERVISOR_PRODUCT_POLICY: Mapping[str, object] = MappingProxyType(dict(_EXPECTED_SUPERVISOR_PRODUCT_POLICY))
SUPERVISOR_PRODUCT_POLICY_SHA256 = _canonical_sha256(dict(_EXPECTED_SUPERVISOR_PRODUCT_POLICY))


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
    tasks = admitted_supervisor_tasks(task_allowlist)
    bounds_are_valid = bool(
        isinstance(max_steps, int)
        and not isinstance(max_steps, bool)
        and max_steps == _EXPECTED_SUPERVISOR_PRODUCT_POLICY["max_steps"]
        and isinstance(max_review_rounds, int)
        and not isinstance(max_review_rounds, bool)
        and max_review_rounds == _EXPECTED_SUPERVISOR_PRODUCT_POLICY["max_review_rounds"]
        and isinstance(timeout_sec, (int, float))
        and not isinstance(timeout_sec, bool)
        and math.isfinite(timeout_sec)
        and 0.1 <= timeout_sec <= 15.0
    )

    reason = SupervisorPolicyClosedReason.ADMITTED
    if normalized_mode == "off":
        reason = SupervisorPolicyClosedReason.MODE_OFF
    elif normalized_mode == "invalid":
        reason = SupervisorPolicyClosedReason.INVALID_MODE
    elif not supervisor_product_policy_is_well_formed():
        reason = SupervisorPolicyClosedReason.POLICY_INVALID
    elif not tasks:
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
        policy_id=SUPERVISOR_PRODUCT_POLICY_ID,
        policy_sha256=SUPERVISOR_PRODUCT_POLICY_SHA256,
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
        max_review_rounds=0,
        timeout_sec=12.0,
        allow_private_text=False,
        secondary_runtime_state="disabled",
        profile_admission="",
        runtime_profile_id="",
        runtime_profile_manifest_sha256="",
    )
