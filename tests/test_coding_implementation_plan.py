from __future__ import annotations

from friday.orchestration.coding_implementation_plan import (
    CodingImplementationPlanReason,
    CodingImplementationPlanState,
    CodingPlanAction,
    build_coding_implementation_plan,
)


def test_create_and_edit_steps_are_planned() -> None:
    result = build_coding_implementation_plan(
        "plan-1",
        "turn-1",
        [
            {"step_id": "readme", "action": "create", "target_path": "README.md"},
            {"step_id": "cli", "action": "edit", "path": "src/cli.py"},
        ],
    )
    assert result.plan is CodingImplementationPlanState.PLANNED
    assert result.steps[0].action is CodingPlanAction.CREATE
    assert result.steps[1].target_path == "src/cli.py"


def test_missing_steps_are_empty() -> None:
    result = build_coding_implementation_plan("plan-1", "turn-1")
    assert result.plan is CodingImplementationPlanState.EMPTY
    assert result.reason is CodingImplementationPlanReason.NO_STEPS
    assert result.steps == ()


def test_execute_actions_are_blocked() -> None:
    result = build_coding_implementation_plan(
        "plan-1",
        "turn-1",
        [{"step_id": "run", "action": "build", "target_path": "src/main.py"}],
    )
    assert result.plan is CodingImplementationPlanState.BLOCKED
    assert result.reason is CodingImplementationPlanReason.EXECUTE_FORBIDDEN
    assert result.steps == ()


def test_absolute_and_traversal_targets_are_blocked() -> None:
    absolute = build_coding_implementation_plan(
        "plan-1",
        "turn-1",
        [{"step_id": "abs", "action": "create", "target_path": "/tmp/x.py"}],
    )
    traversal = build_coding_implementation_plan(
        "plan-2",
        "turn-1",
        [{"step_id": "up", "action": "create", "target_path": "../x.py"}],
    )
    assert absolute.reason is CodingImplementationPlanReason.UNSAFE_TARGET
    assert traversal.reason is CodingImplementationPlanReason.UNSAFE_TARGET
    assert absolute.steps == ()


def test_step_limit_is_blocked() -> None:
    steps = [
        {"step_id": f"s{index}", "action": "create", "target_path": f"f{index}.py"} for index in range(17)
    ]
    result = build_coding_implementation_plan("plan-1", "turn-1", steps)
    assert result.plan is CodingImplementationPlanState.BLOCKED
    assert result.reason is CodingImplementationPlanReason.STEP_LIMIT
