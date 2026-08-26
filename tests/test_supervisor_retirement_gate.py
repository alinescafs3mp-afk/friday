from __future__ import annotations

from dataclasses import replace

import pytest

from friday.orchestration.supervisor_contracts import TaskClass
from friday.orchestration.supervisor_retirement_gate import (
    SUPERVISOR_RETIREMENT_EVIDENCE_SCHEMA,
    HeuristicRetirementCandidate,
    HeuristicRetirementEvidence,
    RetirementEvidenceAuthority,
    RetirementGateReason,
    RetirementSurfaceClass,
    evaluate_heuristic_retirement,
)

POLICY = "1" * 64
MANIFEST = "2" * 64
REGISTRY = "3" * 64
ROLLBACK = "4" * 64


def _candidate(**changes: object) -> HeuristicRetirementCandidate:
    values: dict[str, object] = {
        "candidate_id": "current_file_web.semantic_route_hint",
        "journey": TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
        "surface_class": RetirementSurfaceClass.SEMANTIC_HEURISTIC,
        "replacement_policy_sha256": POLICY,
        "replacement_manifest_sha256": MANIFEST,
        "replacement_adapter_registry_sha256": REGISTRY,
        "rollback_source_sha256": ROLLBACK,
        "documentation_updated": True,
        "status_registry_updated": True,
    }
    values.update(changes)
    return HeuristicRetirementCandidate(**values)  # type: ignore[arg-type]


def _evidence(**changes: object) -> HeuristicRetirementEvidence:
    values: dict[str, object] = {
        "evidence_id": "prod_joined_window_20260826",
        "authority": RetirementEvidenceAuthority.PRODUCTION_JOINED,
        "journey": TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
        "replacement_policy_sha256": POLICY,
        "replacement_manifest_sha256": MANIFEST,
        "replacement_adapter_registry_sha256": REGISTRY,
        "tested_rollback_source_sha256": ROLLBACK,
        "shadow_accepted": True,
        "canary_accepted": True,
        "production_promotion_accepted": True,
        "primary_fallback_proven": True,
        "rollback_proven": True,
        "observation_count": 100,
        "joined_trace_count": 100,
        "hidden_owner_count": 0,
        "duplicate_capability_count": 0,
        "duplicate_effect_count": 0,
        "duplicate_publication_count": 0,
        "false_completion_regression_count": 0,
        "user_visible_regression_count": 0,
    }
    values.update(changes)
    return HeuristicRetirementEvidence(**values)  # type: ignore[arg-type]


def test_exact_production_joined_evidence_admits_review_but_performs_no_deletion() -> None:
    candidate = _candidate()
    evidence = _evidence()

    decision = evaluate_heuristic_retirement(candidate, evidence)

    assert decision.admitted is True
    assert decision.reason is RetirementGateReason.ADMITTED
    assert decision.candidate_id == candidate.candidate_id
    assert decision.evidence_sha256 == evidence.canonical_sha256()
    assert evidence.payload()["schema"] == SUPERVISOR_RETIREMENT_EVIDENCE_SCHEMA


@pytest.mark.parametrize(
    ("candidate", "evidence", "reason"),
    [
        (
            _candidate(surface_class=RetirementSurfaceClass.DETERMINISTIC_INVARIANT),
            _evidence(),
            RetirementGateReason.SURFACE_IS_NOT_SEMANTIC,
        ),
        (
            _candidate(journey=TaskClass.COMPARE_ARCHIVE_WITH_CURRENT_WEB),
            _evidence(journey=TaskClass.COMPARE_ARCHIVE_WITH_CURRENT_WEB),
            RetirementGateReason.JOURNEY_IS_NOT_PROMOTABLE,
        ),
        (
            _candidate(),
            _evidence(journey=TaskClass.COMPARE_ARCHIVE_WITH_CURRENT_WEB),
            RetirementGateReason.JOURNEY_MISMATCH,
        ),
        (
            _candidate(),
            _evidence(replacement_manifest_sha256="5" * 64),
            RetirementGateReason.REPLACEMENT_IDENTITY_MISMATCH,
        ),
        (
            _candidate(),
            _evidence(tested_rollback_source_sha256="6" * 64),
            RetirementGateReason.REPLACEMENT_IDENTITY_MISMATCH,
        ),
        (
            _candidate(),
            _evidence(authority=RetirementEvidenceAuthority.SYNTHETIC_OFFLINE),
            RetirementGateReason.PRODUCTION_EVIDENCE_REQUIRED,
        ),
        (_candidate(), _evidence(shadow_accepted=False), RetirementGateReason.SHADOW_NOT_ACCEPTED),
        (_candidate(), _evidence(canary_accepted=False), RetirementGateReason.CANARY_NOT_ACCEPTED),
        (
            _candidate(),
            _evidence(production_promotion_accepted=False),
            RetirementGateReason.PRODUCTION_PROMOTION_NOT_ACCEPTED,
        ),
        (
            _candidate(),
            _evidence(primary_fallback_proven=False),
            RetirementGateReason.FALLBACK_NOT_PROVEN,
        ),
        (
            _candidate(),
            _evidence(joined_trace_count=99),
            RetirementGateReason.TRACE_COVERAGE_INCOMPLETE,
        ),
        (
            _candidate(),
            _evidence(hidden_owner_count=1),
            RetirementGateReason.HIDDEN_OWNER_OBSERVED,
        ),
        (
            _candidate(),
            _evidence(duplicate_effect_count=1),
            RetirementGateReason.DUPLICATE_OPERATION_OBSERVED,
        ),
        (
            _candidate(),
            _evidence(false_completion_regression_count=1),
            RetirementGateReason.FALSE_COMPLETION_REGRESSION,
        ),
        (
            _candidate(),
            _evidence(user_visible_regression_count=1),
            RetirementGateReason.USER_VISIBLE_REGRESSION,
        ),
        (
            _candidate(),
            _evidence(rollback_proven=False),
            RetirementGateReason.ROLLBACK_NOT_PROVEN,
        ),
        (
            _candidate(documentation_updated=False),
            _evidence(),
            RetirementGateReason.DOCUMENTATION_NOT_UPDATED,
        ),
        (
            _candidate(status_registry_updated=False),
            _evidence(),
            RetirementGateReason.STATUS_REGISTRY_NOT_UPDATED,
        ),
    ],
)
def test_retirement_gate_fails_closed_on_every_architecture_prerequisite(
    candidate: HeuristicRetirementCandidate,
    evidence: HeuristicRetirementEvidence,
    reason: RetirementGateReason,
) -> None:
    decision = evaluate_heuristic_retirement(candidate, evidence)
    assert decision.admitted is False
    assert decision.reason is reason
    assert decision.evidence_sha256 is None


def test_retirement_evidence_is_body_free_and_identity_sensitive() -> None:
    evidence = _evidence()
    payload = evidence.payload()
    assert set(payload) == {
        "schema",
        "evidence_id",
        "authority",
        "journey",
        "replacement_policy_sha256",
        "replacement_manifest_sha256",
        "replacement_adapter_registry_sha256",
        "tested_rollback_source_sha256",
        "shadow_accepted",
        "canary_accepted",
        "production_promotion_accepted",
        "primary_fallback_proven",
        "rollback_proven",
        "observation_count",
        "joined_trace_count",
        "hidden_owner_count",
        "duplicate_capability_count",
        "duplicate_effect_count",
        "duplicate_publication_count",
        "false_completion_regression_count",
        "user_visible_regression_count",
    }
    assert evidence.canonical_sha256() != replace(evidence, observation_count=101).canonical_sha256()


@pytest.mark.parametrize(
    "changes",
    [
        {"observation_count": 0},
        {"joined_trace_count": 101},
        {"hidden_owner_count": -1},
        {"shadow_accepted": 1},
        {"replacement_policy_sha256": "not-a-digest"},
    ],
)
def test_malformed_retirement_evidence_cannot_be_constructed(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _evidence(**changes)
