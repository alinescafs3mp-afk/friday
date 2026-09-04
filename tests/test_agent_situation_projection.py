from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.agent_situation_projection import (
    AgentSituationAudience,
    AgentSituationProjectionState,
    build_agent_situation_projection,
)
from friday.orchestration.shared_operation_view import build_shared_operation_view

OWNER = "a" * 64
CONVERSATION = "b" * 64


def _view() -> object:
    progress = {
        "operation_id": "operation-1",
        "authenticated_turn_id": "turn-1",
        "revision": 1,
        "terminal": False,
        "mode": "coding",
        "title": "Coding operation",
        "ordered_steps": [
            {
                "step_id": "inspect",
                "safe_label": "Inspect project",
                "state": "running",
                "completed_units": None,
                "total_units": None,
                "percentage": None,
                "evidence_class": "sources",
            }
        ],
        "active_step_id": "inspect",
        "elapsed_sec": 8,
        "hard_deadline_remaining_sec": 100,
        "result_delivery_state": "in_flight",
        "plan_generation": 1,
    }
    return build_shared_operation_view(
        "view-1",
        "turn-1",
        progress,
        binding={"owner_digest": OWNER, "conversation_digest": CONVERSATION},
        capability={"capability_id": "coding.inspect", "available": True},
        secondary={"present": True},
        artifacts={
            "artifact_class": "report",
            "artifact_count": 1,
            "artifact_digest": "c" * 64,
        },
    )


def test_empty_situation_has_no_facts() -> None:
    result = build_agent_situation_projection("projection-1", "turn-1")
    assert result.state is AgentSituationProjectionState.EMPTY
    assert result.operation_id is None


def test_primary_projection_is_secret_free_and_preserves_plan() -> None:
    result = build_agent_situation_projection("projection-1", "turn-1", _view(), "primary")
    assert result.state is AgentSituationProjectionState.PROJECTED
    assert result.audience is AgentSituationAudience.PRIMARY
    assert result.operation_id == "operation-1"
    assert result.mode.value == "coding"
    assert len(result.ordered_plan) == 1
    assert result.binding_digest and len(result.binding_digest) == 64
    assert result.artifact_digest and len(result.artifact_digest) == 64
    assert "owner_digest" not in result.to_mapping()
    assert "conversation_digest" not in result.to_mapping()
    assert (
        build_agent_situation_projection(result.to_mapping()).state is AgentSituationProjectionState.PROJECTED
    )


def test_secondary_projection_is_strictly_smaller_and_advisory() -> None:
    primary = build_agent_situation_projection("primary", "turn-1", _view(), "primary")
    secondary = build_agent_situation_projection("secondary", "turn-1", _view(), "secondary")
    assert secondary.state is AgentSituationProjectionState.PROJECTED
    assert secondary.audience is AgentSituationAudience.SECONDARY
    assert secondary.operation_id == primary.operation_id
    assert secondary.ordered_plan == ()
    assert secondary.binding_digest is None
    assert secondary.artifact_digest is None
    assert secondary.secondary_availability is None


def test_blocked_source_stays_blocked_without_leaking_facts() -> None:
    blocked = build_shared_operation_view(
        "view-1",
        "turn-1",
        {
            "operation_id": "operation-1",
            "authenticated_turn_id": "turn-1",
            "revision": 1,
            "terminal": False,
            "mode": "chat",
            "title": "Operation",
            "ordered_steps": [
                {
                    "step_id": "inspect",
                    "safe_label": "Inspect",
                    "state": "running",
                    "completed_units": None,
                    "total_units": None,
                    "percentage": None,
                    "evidence_class": "tasks",
                }
            ],
            "active_step_id": "inspect",
            "elapsed_sec": 1,
            "hard_deadline_remaining_sec": None,
            "result_delivery_state": "in_flight",
            "plan_generation": 1,
        },
        authorized_source_summary=["/private/path"],
    )
    result = build_agent_situation_projection("projection-1", "turn-1", blocked, "primary")
    assert result.state is AgentSituationProjectionState.BLOCKED
    assert result.operation_id is None
    assert result.ordered_plan == ()


def test_situation_is_frozen() -> None:
    result = build_agent_situation_projection("projection-1", "turn-1", _view(), "secondary")
    with pytest.raises(FrozenInstanceError):
        result.operation_id = "other"  # type: ignore[misc]
