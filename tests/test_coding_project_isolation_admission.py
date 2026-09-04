from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.coding_project_isolation_admission import (
    CODING_PROJECT_ISOLATION_ADMISSION_SCHEMA,
    CodingProjectIsolationAdmissionError,
    CodingProjectIsolationAdmissionReason,
    CodingProjectIsolationAdmissionState,
    CodingProjectIsolationAdmissionV1,
    CodingProjectIsolationFactsV1,
    build_coding_project_isolation_admission,
    validate_coding_project_isolation_admission,
)


def test_missing_root_and_destination_are_empty() -> None:
    result = build_coding_project_isolation_admission("isolation:empty", "turn:1")

    assert result.admission is CodingProjectIsolationAdmissionState.EMPTY
    assert result.reason is CodingProjectIsolationAdmissionReason.NO_FACTS
    assert result.project_root is None
    assert result.destination is None


def test_relative_destination_is_admitted_under_supplied_root() -> None:
    result = build_coding_project_isolation_admission(
        "isolation:1",
        "turn:1",
        CodingProjectIsolationFactsV1("/srv/projects/app", "src/main.py"),
    )

    assert result.admission is CodingProjectIsolationAdmissionState.ADMITTED
    assert result.reason is CodingProjectIsolationAdmissionReason.DESTINATION_WITHIN_PROJECT
    assert result.project_root == "/srv/projects/app"
    assert result.destination == "src/main.py"
    with pytest.raises(FrozenInstanceError):
        result.destination = "other.py"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("root", "destination", "reason"),
    (
        (
            "/srv/projects/app",
            "/srv/projects/app/src/main.py",
            CodingProjectIsolationAdmissionReason.ABSOLUTE_DESTINATION,
        ),
        ("/srv/projects/app", "../outside.py", CodingProjectIsolationAdmissionReason.DESTINATION_TRAVERSAL),
        ("/srv/../projects/app", "src/main.py", CodingProjectIsolationAdmissionReason.PROJECT_ROOT_INVALID),
    ),
)
def test_absolute_and_traversal_destinations_fail_closed(
    root: str, destination: str, reason: CodingProjectIsolationAdmissionReason
) -> None:
    result = build_coding_project_isolation_admission(
        "isolation:blocked",
        "turn:1",
        project_root=root,
        destination=destination,
    )

    assert result.admission is CodingProjectIsolationAdmissionState.BLOCKED
    assert result.reason is reason
    assert result.project_root is None
    assert result.destination is None


def test_missing_one_required_fact_is_blocked() -> None:
    missing_root = build_coding_project_isolation_admission(
        "isolation:root", "turn:1", destination="src/main.py"
    )
    missing_destination = build_coding_project_isolation_admission(
        "isolation:destination", "turn:1", project_root="/srv/projects/app"
    )

    assert missing_root.reason is CodingProjectIsolationAdmissionReason.MISSING_PROJECT_ROOT
    assert missing_destination.reason is CodingProjectIsolationAdmissionReason.MISSING_DESTINATION
    assert (
        missing_root.admission
        is missing_destination.admission
        is CodingProjectIsolationAdmissionState.BLOCKED
    )


def test_mapping_and_frozen_facts_are_supported_and_round_trip() -> None:
    result = build_coding_project_isolation_admission(
        {
            "schema": CODING_PROJECT_ISOLATION_ADMISSION_SCHEMA,
            "isolation_id": "isolation:mapping",
            "authenticated_turn_id": "turn:mapping",
            "facts": {"project_root": "project-1", "destination": "src/app.py"},
        }
    )
    encoded = result.to_mapping()

    assert result.admission is CodingProjectIsolationAdmissionState.ADMITTED
    assert build_coding_project_isolation_admission(encoded) == result
    assert validate_coding_project_isolation_admission(encoded) is True


def test_invalid_fact_mapping_and_mixed_representations_fail_closed() -> None:
    invalid = build_coding_project_isolation_admission(
        "isolation:invalid",
        "turn:1",
        facts={"project_root": "/srv/projects/app", "unknown": "x"},
    )
    assert invalid.admission is CodingProjectIsolationAdmissionState.BLOCKED
    assert invalid.reason is CodingProjectIsolationAdmissionReason.INVALID_FACTS

    with pytest.raises(CodingProjectIsolationAdmissionError):
        build_coding_project_isolation_admission(
            {
                "isolation_id": "isolation:mixed",
                "authenticated_turn_id": "turn:1",
                "facts": {"project_root": "project-1", "destination": "src/app.py"},
                "admission": "admitted",
                "reason": "destination_within_project",
            }
        )


def test_validator_rejects_unknown_fields_and_paths_on_blocked_result() -> None:
    result = build_coding_project_isolation_admission(
        "isolation:1", "turn:1", project_root="project-1", destination="src/app.py"
    )
    encoded = result.to_mapping()

    assert validate_coding_project_isolation_admission({**encoded, "extra": "nope"}) is False
    blocked = {
        **encoded,
        "admission": "blocked",
        "project_root": "/srv/projects/app",
        "destination": None,
    }
    assert validate_coding_project_isolation_admission(blocked) is False


def test_direct_result_rejects_exposed_blocked_paths() -> None:
    with pytest.raises(CodingProjectIsolationAdmissionError):
        CodingProjectIsolationAdmissionV1(
            "isolation:bad",
            "turn:1",
            CodingProjectIsolationAdmissionState.BLOCKED,
            "/srv/projects/app",
            None,
            CodingProjectIsolationAdmissionReason.INVALID_FACTS,
        )
