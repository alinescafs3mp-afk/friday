from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.shared_operation_view import (
    SharedOperationViewState,
    build_shared_operation_view,
)

OWNER = "a" * 64
CONVERSATION = "b" * 64
ARTIFACTS = "c" * 64


def _progress() -> dict[str, object]:
    return {
        "operation_id": "operation-1",
        "authenticated_turn_id": "turn-1",
        "revision": 1,
        "terminal": False,
        "mode": "mixed",
        "title": "Shared operation",
        "ordered_steps": [
            {
                "step_id": "inspect",
                "safe_label": "Inspect sources",
                "state": "running",
                "completed_units": None,
                "total_units": None,
                "percentage": None,
                "evidence_class": "sources",
            },
            {
                "step_id": "publish",
                "safe_label": "Publish result",
                "state": "pending",
                "completed_units": None,
                "total_units": None,
                "percentage": None,
                "evidence_class": "stages",
            },
        ],
        "active_step_id": "inspect",
        "elapsed_sec": 12,
        "hard_deadline_remaining_sec": 90,
        "result_delivery_state": "in_flight",
        "plan_generation": 1,
    }


def _projected() -> object:
    return build_shared_operation_view(
        "view-1",
        "turn-1",
        _progress(),
        binding={"owner_digest": OWNER, "conversation_digest": CONVERSATION},
        capability={"capability_id": "files.read", "available": True},
        secondary={"present": False},
        artifacts={
            "artifact_class": "report",
            "artifact_count": 1,
            "artifact_digest": ARTIFACTS,
            "terminal_evidence_class": "unknown",
        },
        authorized_source_summary=["Example.COM", "docs.example.com"],
        effect_owners=["primary"],
        publishers=["telegram"],
    )


def test_empty_view_has_no_operation_facts() -> None:
    result = build_shared_operation_view("view-1", "turn-1")
    assert result.state is SharedOperationViewState.EMPTY
    assert result.operation_id is None
    assert result.ordered_plan == ()


def test_view_composes_identity_plan_deadline_and_body_free_components() -> None:
    result = _projected()
    assert result.state is SharedOperationViewState.PROJECTED
    assert result.operation_id == "operation-1"
    assert result.authenticated_turn_id == "turn-1"
    assert result.mode.value == "mixed"
    assert [step.step_id for step in result.ordered_plan] == ["inspect", "publish"]
    assert result.active_step_id == "inspect"
    assert result.inherited_deadline_remaining_sec == 90
    assert result.authorized_source_summary == ("example.com", "docs.example.com")
    assert result.pending_work_owner.value == "primary"
    assert result.capability.is_available
    assert result.secondary.present is False
    assert result.artifacts.artifact_count == 1
    assert result.binding_digest
    assert build_shared_operation_view(result.to_mapping()).state is SharedOperationViewState.PROJECTED


def test_secondary_absence_keeps_primary_pending_work_owner() -> None:
    result = _projected()
    assert result.secondary.state.value == "absent"
    assert result.pending_work_owner.value == "primary"
    blocked = build_shared_operation_view(
        "view-1",
        "turn-1",
        _progress(),
        secondary={"present": False},
        pending_work_owner="secondary",
    )
    assert blocked.state is SharedOperationViewState.BLOCKED


def test_blocked_inputs_and_multiple_owner_claims_block_view() -> None:
    blocked_component = build_shared_operation_view(
        "view-1", "turn-1", _progress(), capability={"capability_id": "files.read", "available": "maybe"}
    )
    assert blocked_component.state is SharedOperationViewState.BLOCKED
    blocked_owners = build_shared_operation_view(
        "view-1", "turn-1", _progress(), effect_owners=["primary", "secondary"]
    )
    assert blocked_owners.state is SharedOperationViewState.BLOCKED
    blocked_publishers = build_shared_operation_view(
        "view-1", "turn-1", _progress(), publishers=["telegram", "other"]
    )
    assert blocked_publishers.state is SharedOperationViewState.BLOCKED


def test_source_paths_are_not_authorized_hostnames() -> None:
    result = build_shared_operation_view(
        "view-1", "turn-1", _progress(), authorized_source_summary=["https://example.com"]
    )
    assert result.state is SharedOperationViewState.BLOCKED
    assert result.authorized_source_summary == ()


def test_view_is_frozen() -> None:
    result = _projected()
    with pytest.raises(FrozenInstanceError):
        result.operation_id = "other"  # type: ignore[misc]
