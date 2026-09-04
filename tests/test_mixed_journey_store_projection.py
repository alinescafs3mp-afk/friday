from friday.orchestration.mixed_journey_store_projection import (
    MixedJourneyStoreProjectionState,
    build_mixed_journey_store_projection,
)
from friday.orchestration.shared_operation_view import build_shared_operation_view

DIGEST = "d" * 64


def _progress() -> dict[str, object]:
    return {
        "operation_id": "operation-1",
        "authenticated_turn_id": "turn-1",
        "revision": 1,
        "terminal": False,
        "mode": "mixed",
        "title": "Mixed journey",
        "ordered_steps": [
            {"step_id": "run", "safe_label": "Run", "state": "running", "evidence_class": "tasks"}
        ],
        "active_step_id": "run",
        "elapsed_sec": 1,
        "hard_deadline_remaining_sec": 100,
        "result_delivery_state": "in_flight",
        "plan_generation": 1,
    }


def _shared() -> object:
    return build_shared_operation_view(
        "journey-1",
        "turn-1",
        _progress(),
        secondary={"present": False},
        pending_work_owner="primary",
    )


def _facts() -> dict[str, object]:
    return {
        "file": {"file_id": "file-1", "sha256": DIGEST},
        "archive": {"archive_id": "archive-1", "sha256": DIGEST, "member_count": 2},
        "conversation": {"conversation_id": "conversation-1", "authenticated_turn_id": "turn-1"},
        "web": {
            "consumption_id": "web-1",
            "authenticated_turn_id": "turn-1",
            "usability": "consumable",
            "selected_provider_id": "yandex",
            "admitted_source_count": 1,
            "reason": "primary_sources",
        },
        "table": {"table_id": "table-1", "sha256": DIGEST},
        "shared_operation_view": _shared(),
    }


def test_empty_and_projected_projection_round_trip() -> None:
    assert build_mixed_journey_store_projection().state is MixedJourneyStoreProjectionState.EMPTY
    result = build_mixed_journey_store_projection("journey-1", "turn-1", facts=_facts())
    assert result.state is MixedJourneyStoreProjectionState.PROJECTED
    assert result.view is not None
    assert result.shared_operation_view is not None
    assert result.view.coverage.summary_digests
    assert build_mixed_journey_store_projection(result.to_mapping()) == result


def test_absent_organs_do_not_block_and_secondary_absence_keeps_primary_owner() -> None:
    facts = _facts()
    facts.pop("archive")
    facts.pop("conversation")
    facts.pop("web")
    facts.pop("table")
    result = build_mixed_journey_store_projection("journey-1", "turn-1", facts=facts)
    assert result.state is MixedJourneyStoreProjectionState.PROJECTED
    assert result.view is not None
    assert result.view.organs.is_present("file")
    assert result.view.organs.is_present("web") is False
    assert result.view.pending_work_owner.value == "primary"


def test_owner_revoke_and_private_input_hazards_block_without_payload() -> None:
    facts = _facts()
    facts["effect_owners"] = ["primary", "secondary"]
    blocked_owners = build_mixed_journey_store_projection("journey-1", "turn-1", facts=facts)
    assert blocked_owners.state is MixedJourneyStoreProjectionState.BLOCKED
    assert blocked_owners.view is None
    facts = _facts()
    facts["revoke"] = {"revoked": True, "publication_claimed": True}
    blocked_revoke = build_mixed_journey_store_projection("journey-1", "turn-1", facts=facts)
    assert blocked_revoke.state is MixedJourneyStoreProjectionState.BLOCKED
    assert blocked_revoke.view is None
    facts = _facts()
    facts["file"] = {"file_id": "/private/report.pdf", "sha256": DIGEST}
    blocked_private = build_mixed_journey_store_projection("journey-1", "turn-1", facts=facts)
    assert blocked_private.state is MixedJourneyStoreProjectionState.BLOCKED
    assert "/private/report.pdf" not in str(blocked_private.to_mapping())


def test_invalid_facts_fail_closed() -> None:
    result = build_mixed_journey_store_projection(
        "journey-1",
        "turn-1",
        facts={"shared_operation_view": _shared(), "file": {"unknown": object()}},
    )
    assert result.state is MixedJourneyStoreProjectionState.BLOCKED
