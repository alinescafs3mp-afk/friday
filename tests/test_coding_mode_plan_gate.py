from __future__ import annotations

from friday.orchestration.coding_create_admission import build_coding_create_admission
from friday.orchestration.coding_implementation_plan import build_coding_implementation_plan
from friday.orchestration.coding_mode_execute_claim import build_coding_mode_execute_claim
from friday.orchestration.coding_mode_intent import build_coding_mode_intent
from friday.orchestration.coding_mode_plan_gate import (
    CodingModePlanGateReason,
    CodingModePlanGateState,
    build_coding_mode_plan_gate,
)
from friday.orchestration.coding_project_identity import build_coding_project_identity
from friday.orchestration.coding_project_scaffold import build_coding_project_scaffold


def create_admission() -> object:
    turn = "turn-1"
    return build_coding_create_admission(
        "create-1",
        turn,
        identity=build_coding_project_identity(
            "identity-1", turn, project_id="project-1", revision_selector="revision-1"
        ),
        prompt={"title": "project-1", "goal": "create a project"},
        plan=build_coding_implementation_plan(
            "plan-1", turn, [{"step_id": "readme", "action": "create", "target_path": "README.md"}]
        ),
        scaffold=build_coding_project_scaffold("scaffold-1", turn, ["README.md"]),
    )


def test_empty_and_inspect_only() -> None:
    assert build_coding_mode_plan_gate("gate-1", "turn-1").state is CodingModePlanGateState.EMPTY
    intent = build_coding_mode_intent("intent-1", "turn-1", inspect=True)
    execute = build_coding_mode_execute_claim("claim-1", "turn-1", intent)
    result = build_coding_mode_plan_gate("gate-1", "turn-1", intent, execute)
    assert result.state is CodingModePlanGateState.INSPECT_ONLY


def test_create_consumes_create_admission() -> None:
    intent = build_coding_mode_intent("intent-1", "turn-1", prompt="create project")
    result = build_coding_mode_plan_gate(
        "gate-1",
        "turn-1",
        intent,
        build_coding_mode_execute_claim("claim-1", "turn-1", intent),
        create_admission(),
    )
    assert result.state is CodingModePlanGateState.CREATE
    assert result.admission_id == "create-1"


def test_blocked_admission_and_execute_without_worker_fail_closed() -> None:
    intent = build_coding_mode_intent("intent-1", "turn-1", prompt="create project")
    blocked = build_coding_mode_plan_gate(
        "gate-1",
        "turn-1",
        intent,
        create_admission={
            "admission_id": "bad",
            "authenticated_turn_id": "turn-1",
            "admission": "blocked",
            "project_id": None,
            "revision_selector": None,
            "reason": "invalid_facts",
        },
    )
    assert blocked.state is CodingModePlanGateState.BLOCKED
    execute = build_coding_mode_execute_claim("claim-1", "turn-1", intent, operation="build")
    result = build_coding_mode_plan_gate("gate-1", "turn-1", intent, execute, create_admission())
    assert result.state is CodingModePlanGateState.BLOCKED
    assert result.reason is CodingModePlanGateReason.EXECUTE_CLAIM_BLOCKED


def test_mapping_roundtrip() -> None:
    intent = build_coding_mode_intent("intent-1", "turn-1", inspect=True)
    result = build_coding_mode_plan_gate(
        "gate-1", "turn-1", intent, build_coding_mode_execute_claim("claim-1", "turn-1", intent)
    )
    assert build_coding_mode_plan_gate(result.to_mapping()) == result
