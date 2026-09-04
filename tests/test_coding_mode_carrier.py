from __future__ import annotations

from friday.orchestration.coding_mode_carrier import (
    CodingModeCarrierReason,
    CodingModeCarrierState,
    build_coding_mode_carrier,
)
from friday.orchestration.coding_result_archive_manifest import build_coding_result_archive_manifest
from friday.orchestration.coding_result_archive_pack_admission import (
    build_coding_result_archive_pack_admission,
)
from friday.orchestration.coding_result_archive_plan import build_coding_result_archive_plan
from friday.orchestration.coding_result_publication_admission import build_coding_result_publication_admission
from friday.orchestration.coding_result_uncertainty import build_coding_result_uncertainty

SHA256 = "a" * 64


def archive_facts() -> tuple[object, object, object, object]:
    plan = build_coding_result_archive_plan("plan-1", "turn-1", files=["a.py", "b.py"])
    manifest = build_coding_result_archive_manifest("manifest-1", "turn-1", {"a.py": SHA256, "b.py": SHA256})
    pack = build_coding_result_archive_pack_admission("pack-1", "turn-1", plan, manifest)
    uncertainty = build_coding_result_uncertainty("uncertainty-1", "turn-1", plan, pack)
    publication = build_coding_result_publication_admission(
        "publication-1", "turn-1", plan, pack, uncertainty=uncertainty
    )
    return plan, pack, uncertainty, publication


def test_empty_text_file_and_archive_carriers() -> None:
    assert build_coding_mode_carrier("carrier-1", "turn-1").state is CodingModeCarrierState.EMPTY
    empty_plan = build_coding_result_archive_plan("plan-1", "turn-1")
    assert (
        build_coding_mode_carrier("carrier-1", "turn-1", archive_plan=empty_plan).state
        is CodingModeCarrierState.TEXT
    )
    plan = build_coding_result_archive_plan("plan-1", "turn-1", files=["main.py"])
    uncertainty = build_coding_result_uncertainty("uncertainty-1", "turn-1", plan)
    publication = build_coding_result_publication_admission(
        "publication-1", "turn-1", plan, uncertainty=uncertainty
    )
    assert build_coding_mode_carrier("carrier-1", "turn-1", publication).state is CodingModeCarrierState.FILE
    plan, pack, uncertainty, publication = archive_facts()
    result = build_coding_mode_carrier("carrier-1", "turn-1", publication, plan, pack, uncertainty)
    assert result.state is CodingModeCarrierState.ARCHIVE


def test_unknown_uncertainty_and_blocked_publication_are_not_carriers() -> None:
    plan, _, _, _ = archive_facts()
    unknown = build_coding_result_uncertainty("uncertainty-1", "turn-1", plan)
    result = build_coding_mode_carrier("carrier-1", "turn-1", archive_plan=plan, uncertainty=unknown)
    assert result.state is CodingModeCarrierState.BLOCKED
    assert result.reason in {
        CodingModeCarrierReason.PUBLICATION_BLOCKED,
        CodingModeCarrierReason.UNCERTAINTY_UNKNOWN,
    }


def test_mapping_roundtrip() -> None:
    _, _, _, publication = archive_facts()
    result = build_coding_mode_carrier("carrier-1", "turn-1", publication)
    assert build_coding_mode_carrier(result.to_mapping()) == result
