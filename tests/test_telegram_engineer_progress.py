"""Pure Engineer N1 progress and final-carrier contracts."""

from __future__ import annotations

import pytest

from friday.orchestration.operation_progress import render_operation_progress as shared_renderer
from friday.telegram_bridge._engineer_progress import (
    EngineerProgressStage,
    EngineerResultCarrierKind,
    EngineerResultPolicyError,
    build_engineer_operation_progress,
    build_engineer_running_progress,
    can_build_engineer_archive,
    render_operation_progress,
    select_engineer_result_carrier,
    select_user_result_files,
    validate_engineer_result_carrier,
)


def test_engineer_running_projection_is_engineer_mode_one_focus_without_eta() -> None:
    projection = build_engineer_running_progress(
        75,
        operation_id="engineer:job-1",
        authenticated_turn_id="turn-1",
        revision=2,
        timeout_sec=300,
        stdout_bytes=2048,
        stderr_bytes=17,
    )

    assert projection.mode.value == "engineer"
    assert projection.active_step_id == "command"
    assert [step.step_id for step in projection.ordered_steps if step.state.value == "running"] == ["command"]
    running = projection.ordered_steps[0]
    assert running.percentage is None
    assert running.total_units is None
    text = render_operation_progress(projection)
    assert "stdout 2.0 КиБ" in text
    assert "stderr 17 Б" in text
    assert "Прошло: 1 мин 15 с" in text
    assert "Тайм-аут: осталось 3 мин 45 с" in text
    assert "ETA" not in text
    assert "процент" not in text.casefold()


def test_engineer_mapping_builder_accepts_existing_status_update_shape() -> None:
    projection = build_engineer_operation_progress(
        {
            "status_update": {
                "operation_id": "engineer:abc",
                "authenticated_turn_id": "turn-abc",
                "revision": 4,
                "stage": "command_running",
                "elapsed_sec": 42,
                "timeout_sec": 0,
                "remaining_sec": None,
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "output_activity": False,
            }
        }
    )

    assert projection.operation_id == "engineer:abc"
    assert projection.authenticated_turn_id == "turn-abc"
    assert projection.hard_deadline_remaining_sec is None
    assert projection.result_delivery_state.value == "not_started"
    assert "текстового вывода ещё не было" in render_operation_progress(projection)


@pytest.mark.parametrize(
    ("stage", "prefix", "delivery"),
    (
        (EngineerProgressStage.COMPLETED, "✅", "confirmed"),
        (EngineerProgressStage.FAILED, "❌", "failed"),
        (EngineerProgressStage.UNKNOWN, "⚠️", "uncertain"),
        (EngineerProgressStage.CANCELLED, "⏹", "failed"),
    ),
)
def test_engineer_terminal_projection_uses_one_shared_renderer(
    stage: EngineerProgressStage,
    prefix: str,
    delivery: str,
) -> None:
    projection = build_engineer_operation_progress(stage, 12)

    assert projection.terminal is True
    assert projection.result_delivery_state.value == delivery
    assert render_operation_progress(projection).startswith(prefix)
    assert render_operation_progress is shared_renderer


def test_delivering_result_has_only_delivery_step_running() -> None:
    projection = build_engineer_operation_progress(EngineerProgressStage.DELIVERING_RESULT, 18)

    assert projection.active_step_id == "result_delivery"
    assert [step.step_id for step in projection.ordered_steps if step.state.value == "running"] == [
        "result_delivery"
    ]
    assert all(step.percentage is None for step in projection.ordered_steps if step.state.value == "running")


def test_empty_and_single_ordinary_archives_are_rejected() -> None:
    with pytest.raises(EngineerResultPolicyError, match="empty_archive"):
        validate_engineer_result_carrier(EngineerResultCarrierKind.ARCHIVE, [])
    with pytest.raises(EngineerResultPolicyError, match="single_ordinary_file_archive_forbidden"):
        validate_engineer_result_carrier(EngineerResultCarrierKind.ARCHIVE, ["result.txt"])

    assert can_build_engineer_archive([]) is False
    assert can_build_engineer_archive(["result.txt"]) is False


def test_carrier_selection_uses_text_file_or_archive_without_empty_or_one_file_zip() -> None:
    assert select_engineer_result_carrier([]).carrier is EngineerResultCarrierKind.TEXT
    one = select_engineer_result_carrier(["result.txt"], requested="archive")
    assert one.carrier is EngineerResultCarrierKind.FILE
    assert [item.relative_path for item in one.files] == ["result.txt"]
    many = select_engineer_result_carrier(["b.txt", "a.txt"], requested="archive")
    assert many.carrier is EngineerResultCarrierKind.ARCHIVE
    assert [item.relative_path for item in many.files] == ["a.txt", "b.txt"]


def test_internal_command_evidence_is_hidden_unless_explicitly_requested() -> None:
    files = [
        "outputs/result.txt",
        "RECEIPT.json",
        "MANIFEST.json",
        "logs/command.log",
        "tmp/stdout.bin",
        "cache/index.json",
    ]

    visible = select_user_result_files(files)
    assert [item.relative_path for item in visible] == ["outputs/result.txt"]
    explicit = select_user_result_files(files, include_internal=True)
    assert {item.relative_path for item in explicit} == set(files)
    assert select_engineer_result_carrier(files).carrier is EngineerResultCarrierKind.FILE
    assert (
        select_engineer_result_carrier(files, include_internal=True).carrier
        is EngineerResultCarrierKind.ARCHIVE
    )


def test_carrier_policy_is_owned_by_orchestration() -> None:
    from friday.orchestration import engineer_result_carrier as orchestration
    from friday.telegram_bridge import _engineer_progress as seam

    assert seam.select_engineer_result_carrier is orchestration.select_engineer_result_carrier
    assert seam.EngineerResultCarrierKind is orchestration.EngineerResultCarrierKind


def test_result_path_and_duplicates_fail_closed() -> None:
    with pytest.raises(EngineerResultPolicyError, match="result_path_invalid"):
        select_user_result_files(["../escape.txt"])
    with pytest.raises(EngineerResultPolicyError, match="result_path_invalid"):
        select_user_result_files(["/absolute.txt"])
    with pytest.raises(EngineerResultPolicyError, match="result_file_duplicate"):
        select_user_result_files(["result.txt", "RESULT.TXT"])
