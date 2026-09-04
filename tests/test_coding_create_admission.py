from __future__ import annotations

from friday.orchestration.coding_create_admission import (
    CodingCreateAdmissionReason,
    CodingCreateAdmissionState,
    build_coding_create_admission,
)
from friday.orchestration.coding_implementation_plan import build_coding_implementation_plan
from friday.orchestration.coding_project_identity import build_coding_project_identity
from friday.orchestration.coding_project_scaffold import build_coding_project_scaffold
from friday.orchestration.coding_prompt_normalization import build_coding_prompt_normalization


def _admitted_inputs() -> dict[str, object]:
    turn = "turn-1"
    return {
        "identity": build_coding_project_identity(
            "id-1", turn, project_id="photo-indexer", revision_selector="rev-1"
        ),
        "prompt": build_coding_prompt_normalization(
            "prompt-1", turn, title="photo-indexer", goal="Index local photos by date"
        ),
        "plan": build_coding_implementation_plan(
            "plan-1",
            turn,
            [{"step_id": "readme", "action": "create", "target_path": "README.md"}],
        ),
        "scaffold": build_coding_project_scaffold("scaf-1", turn, ["README.md"]),
    }


def test_all_gates_admit_create() -> None:
    result = build_coding_create_admission("adm-1", "turn-1", **_admitted_inputs())
    assert result.admission is CodingCreateAdmissionState.ADMITTED
    assert result.project_id == "photo-indexer"
    assert result.revision_selector == "rev-1"


def test_missing_inputs_are_empty() -> None:
    result = build_coding_create_admission("adm-1", "turn-1")
    assert result.admission is CodingCreateAdmissionState.EMPTY
    assert result.project_id is None


def test_recency_identity_blocks_without_exposing_project() -> None:
    inputs = _admitted_inputs()
    inputs["identity"] = build_coding_project_identity(
        "id-1", "turn-1", project_id="photo-indexer", revision_selector="latest"
    )
    result = build_coding_create_admission("adm-1", "turn-1", **inputs)
    assert result.admission is CodingCreateAdmissionState.BLOCKED
    assert result.reason is CodingCreateAdmissionReason.IDENTITY_NOT_IDENTIFIED
    assert result.project_id is None


def test_turn_mismatch_is_blocked() -> None:
    inputs = _admitted_inputs()
    inputs["prompt"] = build_coding_prompt_normalization(
        "prompt-1", "turn-other", title="photo-indexer", goal="Index local photos by date"
    )
    result = build_coding_create_admission("adm-1", "turn-1", **inputs)
    assert result.reason is CodingCreateAdmissionReason.IDENTITY_MISMATCH
    assert result.project_id is None
