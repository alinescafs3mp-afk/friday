from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.coding_archive_extract_admission import (
    CodingArchiveFileKind,
    CodingArchiveLinkKind,
    CodingArchiveMemberV1,
    build_coding_archive_extract_admission,
)
from friday.orchestration.coding_archive_extract_plan import build_coding_archive_extract_plan
from friday.orchestration.coding_archive_member_catalog import build_coding_archive_member_catalog
from friday.orchestration.coding_archive_overwrite_plan import (
    CodingArchiveExistingDestinationFactV1,
    build_coding_archive_overwrite_plan,
)
from friday.orchestration.coding_implementation_plan import build_coding_implementation_plan
from friday.orchestration.coding_inspect_report import build_coding_inspect_report
from friday.orchestration.coding_project_identity import build_coding_project_identity
from friday.orchestration.coding_project_isolation_admission import (
    CodingProjectIsolationFactsV1,
    build_coding_project_isolation_admission,
)
from friday.orchestration.coding_source_member import CodingSourceMemberV1
from friday.orchestration.coding_upload_modification_admission import (
    CODING_UPLOAD_MODIFICATION_ADMISSION_SCHEMA,
    CodingUploadModificationAdmissionReason,
    CodingUploadModificationAdmissionState,
    build_coding_upload_modification_admission,
    validate_coding_upload_modification_admission,
)

TURN = "turn-1"


def source_member(path: str = "src/main.py") -> CodingSourceMemberV1:
    return CodingSourceMemberV1(path, 4, "file", False, "none")


def archive_member(path: str = "src/main.py") -> CodingArchiveMemberV1:
    return CodingArchiveMemberV1(
        path,
        compressed_size=100,
        uncompressed_size=1_000,
        link_kind=CodingArchiveLinkKind.NONE,
        file_kind=CodingArchiveFileKind.REGULAR_FILE,
    )


def _admitted_inputs(*, secret_name: bool = False, edit: bool = True) -> dict[str, object]:
    members = (source_member(".env" if secret_name else "src/main.py"),)
    steps = (
        [{"step_id": "edit_main", "action": "edit", "target_path": "src/main.py"}]
        if edit
        else [{"step_id": "readme", "action": "create", "target_path": "README.md"}]
    )
    return {
        "identity": build_coding_project_identity(
            "id-1", TURN, project_id="photo-indexer", revision_selector="rev-1"
        ),
        "inspect_report": build_coding_inspect_report("report-1", TURN, members=members),
        "isolation": build_coding_project_isolation_admission(
            "isolation-1",
            TURN,
            CodingProjectIsolationFactsV1("/srv/projects/app", "src/main.py"),
        ),
        "plan": build_coding_implementation_plan("plan-1", TURN, steps),
    }


def _extract_family(*, existing: bool = False) -> dict[str, object]:
    members = (archive_member(),)
    catalog = build_coding_archive_member_catalog("catalog:1", TURN, members)
    admission = build_coding_archive_extract_admission("extract-adm:1", TURN, members)
    plan = build_coding_archive_extract_plan("extract:1", TURN, catalog, admission)
    existing_facts = (
        (CodingArchiveExistingDestinationFactV1("src/main.py", True),) if existing else ()
    )
    return {
        "extract_admission": admission,
        "extract_plan": plan,
        "overwrite_plan": build_coding_archive_overwrite_plan(
            "overwrite:1", TURN, plan, existing_facts
        ),
    }


def test_mapped_tree_edit_is_admitted() -> None:
    result = build_coding_upload_modification_admission("adm-1", TURN, **_admitted_inputs())
    assert result.admission is CodingUploadModificationAdmissionState.ADMITTED
    assert result.project_id == "photo-indexer"
    assert result.revision_selector == "rev-1"
    assert result.reason is CodingUploadModificationAdmissionReason.ALL_GATES_ADMITTED
    with pytest.raises(FrozenInstanceError):
        result.project_id = "other"  # type: ignore[misc]


def test_archive_extract_family_is_admitted_when_overwrite_is_clear() -> None:
    result = build_coding_upload_modification_admission(
        "adm-1", TURN, **_admitted_inputs(), **_extract_family()
    )
    assert result.admission is CodingUploadModificationAdmissionState.ADMITTED
    assert result.project_id == "photo-indexer"


def test_missing_inputs_are_empty() -> None:
    result = build_coding_upload_modification_admission("adm-1", TURN)
    assert result.admission is CodingUploadModificationAdmissionState.EMPTY
    assert result.project_id is None
    assert result.reason is CodingUploadModificationAdmissionReason.NO_FACTS


def test_recency_identity_blocks_without_exposing_project() -> None:
    inputs = _admitted_inputs()
    inputs["identity"] = build_coding_project_identity(
        "id-1", TURN, project_id="photo-indexer", revision_selector="latest"
    )
    result = build_coding_upload_modification_admission("adm-1", TURN, **inputs)
    assert result.admission is CodingUploadModificationAdmissionState.BLOCKED
    assert result.reason is CodingUploadModificationAdmissionReason.IDENTITY_NOT_IDENTIFIED
    assert result.project_id is None


def test_secret_name_in_upload_blocks_modification() -> None:
    result = build_coding_upload_modification_admission(
        "adm-1", TURN, **_admitted_inputs(secret_name=True)
    )
    assert result.admission is CodingUploadModificationAdmissionState.BLOCKED
    assert result.reason is CodingUploadModificationAdmissionReason.HAZARDS_PRESENT
    assert result.project_id is None


def test_create_only_plan_is_not_a_modification() -> None:
    result = build_coding_upload_modification_admission(
        "adm-1", TURN, **_admitted_inputs(edit=False)
    )
    assert result.reason is CodingUploadModificationAdmissionReason.PLAN_HAS_NO_EDIT
    assert result.project_id is None


def test_turn_mismatch_is_blocked() -> None:
    inputs = _admitted_inputs()
    inputs["isolation"] = build_coding_project_isolation_admission(
        "isolation-1",
        "turn-other",
        CodingProjectIsolationFactsV1("/srv/projects/app", "src/main.py"),
    )
    result = build_coding_upload_modification_admission("adm-1", TURN, **inputs)
    assert result.reason is CodingUploadModificationAdmissionReason.IDENTITY_MISMATCH
    assert result.project_id is None


def test_partial_extract_family_is_blocked() -> None:
    inputs = _admitted_inputs()
    family = _extract_family()
    result = build_coding_upload_modification_admission(
        "adm-1",
        TURN,
        **inputs,
        extract_admission=family["extract_admission"],
    )
    assert result.reason is CodingUploadModificationAdmissionReason.EXTRACT_FAMILY_INCOMPLETE
    assert result.project_id is None


def test_existing_extract_destination_blocks_overwrite() -> None:
    result = build_coding_upload_modification_admission(
        "adm-1", TURN, **_admitted_inputs(), **_extract_family(existing=True)
    )
    assert result.reason is CodingUploadModificationAdmissionReason.OVERWRITE_NOT_CLEAR
    assert result.project_id is None


def test_mapping_roundtrip_validates() -> None:
    result = build_coding_upload_modification_admission("adm-1", TURN, **_admitted_inputs())
    encoded = result.to_mapping()
    assert encoded["schema"] == CODING_UPLOAD_MODIFICATION_ADMISSION_SCHEMA
    assert validate_coding_upload_modification_admission(result) is True
    assert validate_coding_upload_modification_admission(encoded) is True
    assert validate_coding_upload_modification_admission({**encoded, "extra": "nope"}) is False
    blocked = build_coding_upload_modification_admission(
        "adm-1", TURN, **_admitted_inputs(secret_name=True)
    )
    assert validate_coding_upload_modification_admission(blocked.to_mapping()) is True
    assert validate_coding_upload_modification_admission({**encoded, "project_id": None}) is False
