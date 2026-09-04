from hashlib import sha256

import pytest

from friday.orchestration.mixed_journey_coverage import build_mixed_journey_coverage
from friday.orchestration.mixed_journey_identity import build_mixed_journey_identity
from friday.orchestration.mixed_journey_organs import build_mixed_journey_organs
from friday.orchestration.mixed_journey_restart import build_mixed_journey_restart
from friday.orchestration.mixed_journey_revoke import build_mixed_journey_revoke
from friday.orchestration.mixed_journey_view import MixedJourneyViewState, build_mixed_journey_view
from friday.orchestration.shared_operation_view import build_shared_operation_view


def progress() -> dict[str, object]:
    return {
        "operation_id": "operation",
        "authenticated_turn_id": "turn",
        "revision": 1,
        "terminal": False,
        "mode": "mixed",
        "title": "mixed journey",
        "ordered_steps": [
            {"step_id": "run", "safe_label": "Run", "state": "running", "evidence_class": "tasks"}
        ],
        "active_step_id": "run",
        "elapsed_sec": 1,
        "hard_deadline_remaining_sec": 100,
        "result_delivery_state": "in_flight",
        "plan_generation": 1,
    }


def components():
    organs = build_mixed_journey_organs(
        "view",
        "turn",
        facts={
            "file": True,
            "archive": True,
            "conversation": True,
            "web": True,
            "table": True,
            "engineer": False,
            "coding": False,
        },
    )
    digests = {
        name: sha256(name.encode()).hexdigest()
        for name in ("file", "archive", "conversation", "web", "table")
    }
    return {
        "identity": build_mixed_journey_identity("view", "turn", facts={"operation_id": "operation"}),
        "organs": organs,
        "coverage": build_mixed_journey_coverage("view", "turn", organs, digests),
        "revoke": build_mixed_journey_revoke(
            "view", "turn", facts={"revoked": False, "publication_claimed": True}
        ),
        "restart": build_mixed_journey_restart(
            "view", "turn", facts={"status": "running", "execution": "running"}
        ),
    }


def test_empty_and_projected_view_compose_primary_and_secondary() -> None:
    assert build_mixed_journey_view("view", "turn").state is MixedJourneyViewState.EMPTY
    shared = build_shared_operation_view("view", "turn", progress())
    result = build_mixed_journey_view("view", "turn", shared, facts=components())
    assert result.state is MixedJourneyViewState.PROJECTED
    assert result.primary.state.value == "projected"
    assert result.secondary.state.value == "projected"
    assert result.primary.ordered_plan
    assert result.secondary.ordered_plan == ()
    assert build_mixed_journey_view(result.to_mapping()) == result


@pytest.mark.parametrize(
    "change",
    [
        {
            "revoke": build_mixed_journey_revoke(
                "view", "turn", facts={"revoked": True, "publication_claimed": True}
            )
        },
        {
            "identity": build_mixed_journey_identity(
                "view", "turn", facts={"operation_id": "operation", "publishers": ["one", "two"]}
            )
        },
        {
            "restart": build_mixed_journey_restart(
                "view",
                "turn",
                facts={"status": "running", "execution": "running", "effect_owners": ["one", "two"]},
            )
        },
    ],
)
def test_view_blocks_revoke_multiple_publisher_and_owner_hazards(change: dict[str, object]) -> None:
    values = components()
    values.update(change)
    shared = build_shared_operation_view("view", "turn", progress())
    result = build_mixed_journey_view("view", "turn", shared, facts=values)
    assert result.state is MixedJourneyViewState.BLOCKED
    assert result.publication_claimed is False
    assert result.publisher_count == 0


def test_view_rejects_missing_component_and_forwards_shared_block() -> None:
    values = components()
    values.pop("restart")
    shared = build_shared_operation_view("view", "turn", progress())
    assert (
        build_mixed_journey_view("view", "turn", shared, facts=values).state is MixedJourneyViewState.BLOCKED
    )
    values = components()
    values["shared_operation_view"] = {
        "view_id": "view",
        "authenticated_turn_id": "turn",
        "view": "blocked",
        "reason": "progress_invalid",
    }
    assert build_mixed_journey_view("view", "turn", facts=values).state is MixedJourneyViewState.BLOCKED


def test_absent_secondary_preserves_primary_ownership() -> None:
    values = components()
    shared = build_shared_operation_view(
        "view", "turn", progress(), secondary={"present": False}, pending_work_owner="primary"
    )
    result = build_mixed_journey_view("view", "turn", shared, facts=values)
    assert result.state is MixedJourneyViewState.PROJECTED
    assert result.pending_work_owner.value == "primary"
    assert result.secondary_situation.ordered_plan == ()
