from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.coding_result_archive_manifest import (
    CodingResultArchiveManifestState,
    build_coding_result_archive_manifest,
)
from friday.orchestration.coding_result_archive_pack_admission import (
    CODING_RESULT_ARCHIVE_PACK_ADMISSION_SCHEMA,
    CodingResultArchivePackAdmissionReason,
    CodingResultArchivePackAdmissionState,
    build_coding_result_archive_pack_admission,
    validate_coding_result_archive_pack_admission,
)
from friday.orchestration.coding_result_archive_plan import (
    CodingResultArchivePlanState,
    build_coding_result_archive_plan,
)

SHA256 = "a" * 64


def _plan(files: object) -> object:
    return build_coding_result_archive_plan("plan-1", "turn-1", files=files)


def _manifest(files: object) -> object:
    return build_coding_result_archive_manifest("manifest-1", "turn-1", files)


def test_no_plan_or_manifest_is_empty() -> None:
    result = build_coding_result_archive_pack_admission("pack-1", "turn-1")

    assert result.admission is CodingResultArchivePackAdmissionState.EMPTY


def test_complete_archive_plan_and_manifest_are_admitted() -> None:
    result = build_coding_result_archive_pack_admission(
        "pack-1",
        "turn-1",
        _plan(["a.py", "b.py"]),
        _manifest({"a.py": SHA256, "b.py": "b" * 64}),
    )

    assert result.admission is CodingResultArchivePackAdmissionState.ADMITTED
    assert result.member_paths == ("a.py", "b.py")
    assert result.archive_filename == "friday-source.zip"


def test_file_plan_is_never_wrapped_in_a_one_file_zip() -> None:
    result = build_coding_result_archive_pack_admission(
        "pack-1", "turn-1", _plan(["only.py"]), _manifest({"only.py": SHA256})
    )

    assert result.admission is CodingResultArchivePackAdmissionState.BLOCKED
    assert result.reason is CodingResultArchivePackAdmissionReason.ONE_FILE_ARCHIVE_FORBIDDEN
    assert result.member_paths == ()


def test_empty_and_blocked_plans_are_not_packable() -> None:
    empty = build_coding_result_archive_pack_admission("pack-1", "turn-1", _plan(None), _manifest(None))
    blocked = build_coding_result_archive_pack_admission(
        "pack-1", "turn-1", _plan(["../escape.py"]), _manifest({"a.py": SHA256})
    )

    assert empty.reason is CodingResultArchivePackAdmissionReason.PLAN_NOT_ARCHIVE
    assert blocked.reason is CodingResultArchivePackAdmissionReason.PLAN_BLOCKED


def test_manifest_must_list_every_planned_member() -> None:
    result = build_coding_result_archive_pack_admission(
        "pack-1", "turn-1", _plan(["a.py", "b.py"]), _manifest({"a.py": SHA256})
    )

    assert result.admission is CodingResultArchivePackAdmissionState.BLOCKED
    assert result.reason is CodingResultArchivePackAdmissionReason.MANIFEST_MISMATCH


def test_turn_mismatch_and_blocked_manifest_fail_closed() -> None:
    mismatch = build_coding_result_archive_pack_admission(
        "pack-1",
        "turn-1",
        _plan(["a.py", "b.py"]),
        build_coding_result_archive_manifest("manifest-1", "turn-other", {"a.py": SHA256, "b.py": SHA256}),
    )
    blocked_manifest = build_coding_result_archive_pack_admission(
        "pack-1",
        "turn-1",
        _plan(["a.py", "b.py"]),
        build_coding_result_archive_manifest("manifest-1", "turn-1", {"a.py": "bad", "b.py": SHA256}),
    )

    assert mismatch.reason is CodingResultArchivePackAdmissionReason.IDENTITY_MISMATCH
    assert blocked_manifest.reason is CodingResultArchivePackAdmissionReason.MANIFEST_BLOCKED
    assert blocked_manifest.member_paths == ()


def test_mapping_roundtrip_and_frozen_contract() -> None:
    result = build_coding_result_archive_pack_admission(
        "pack-1", "turn-1", _plan(["a.py", "b.py"]), _manifest({"a.py": SHA256, "b.py": SHA256})
    )
    encoded = result.to_mapping()

    assert encoded["schema"] == CODING_RESULT_ARCHIVE_PACK_ADMISSION_SCHEMA
    assert validate_coding_result_archive_pack_admission(encoded) is True
    assert result.member_paths == ("a.py", "b.py")
    with pytest.raises(FrozenInstanceError):
        result.admission = CodingResultArchivePackAdmissionState.EMPTY  # type: ignore[misc]


def test_manifest_empty_state_is_not_listed() -> None:
    manifest = _manifest(None)
    assert manifest.manifest is CodingResultArchiveManifestState.EMPTY
    result = build_coding_result_archive_pack_admission("pack-1", "turn-1", _plan(["a.py", "b.py"]), manifest)
    assert result.reason is CodingResultArchivePackAdmissionReason.MANIFEST_NOT_LISTED


def test_plan_state_is_archive_for_two_files() -> None:
    assert _plan(["a.py", "b.py"]).plan is CodingResultArchivePlanState.ARCHIVE
