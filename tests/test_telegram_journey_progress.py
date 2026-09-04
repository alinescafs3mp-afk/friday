"""Truthful operation progress for files, archives and public web research."""

from __future__ import annotations

import re

import pytest

from friday.orchestration.operation_progress import OperationMode, OperationStepState
from friday.telegram_bridge._journey_status import (
    ArchiveStatusStage,
    FilesStatusStage,
    WebStatusStage,
    build_archive_operation_progress,
    build_files_operation_progress,
    build_web_operation_progress,
    render_operation_progress,
)
from friday.telegram_bridge._status import TelegramStatusStage, render_interactive_turn_status


@pytest.mark.parametrize(
    ("builder", "stage", "kwargs", "mode", "expected_step"),
    [
        pytest.param(
            build_files_operation_progress,
            FilesStatusStage.PROCESSING_DOCUMENTS,
            {"file_total": 4, "received_files": 4, "processed_files": 1},
            OperationMode.DOCUMENT,
            "document_processing",
            id="files",
        ),
        pytest.param(
            build_archive_operation_progress,
            ArchiveStatusStage.BUILDING_ARCHIVE,
            {"file_total": 5, "selected_files": 5, "archived_files": 2},
            OperationMode.DOCUMENT,
            "archive_building",
            id="archive",
        ),
        pytest.param(
            build_web_operation_progress,
            WebStatusStage.READING_SOURCES,
            {"source_total": 4, "discovered_sources": 4, "reviewed_sources": 2},
            OperationMode.RESEARCH,
            "source_review",
            id="web",
        ),
    ],
)
def test_running_journeys_use_measured_counts_without_fabricated_percent(
    builder,
    stage,
    kwargs,
    mode,
    expected_step,
) -> None:
    projection = builder(stage, 42, **kwargs)
    text = render_operation_progress(projection)

    assert projection.mode is mode
    assert projection.active_step_id == expected_step
    assert [step.step_id for step in projection.ordered_steps].count(expected_step) == 1
    assert sum(step.state is OperationStepState.RUNNING for step in projection.ordered_steps) == 1
    running = next(step for step in projection.ordered_steps if step.state is OperationStepState.RUNNING)
    assert running.percentage is None
    assert "из" in text
    assert "ETA" not in text
    assert "Тайм-аут" not in text
    assert "Прошло: 42 с" in text


def test_files_projection_keeps_open_ended_steps_without_percent() -> None:
    projection = build_files_operation_progress(
        FilesStatusStage.DELIVERING_RESULT,
        9,
        file_total=3,
        received_files=3,
        processed_files=3,
    )

    assert projection.active_step_id == "file_result"
    assert projection.ordered_steps[-1].percentage is None
    assert "▶️ **отправляю готовый результат**" in render_operation_progress(projection)


def test_completed_and_pending_steps_may_show_only_boundary_percentages() -> None:
    projection = build_web_operation_progress(
        WebStatusStage.FORMULATING_ANSWER,
        61,
        source_total=3,
        discovered_sources=3,
        reviewed_sources=3,
    )
    text = render_operation_progress(projection)

    assert projection.ordered_steps[0].percentage == 100
    assert projection.ordered_steps[1].percentage == 100
    assert projection.ordered_steps[2].percentage is None
    assert "100%" in text
    assert "0%" not in re.findall(r"(?<!\d)0%(?!\d)", text)
    assert all(value in {"0", "100"} for value in re.findall(r"(\d+)%", text))


def test_journey_step_ids_are_distinct_from_chat_stage_ids_and_each_other() -> None:
    projections = (
        build_files_operation_progress(FilesStatusStage.RECEIVING_FILES, 1),
        build_archive_operation_progress(ArchiveStatusStage.SELECTING_FILES, 1),
        build_web_operation_progress(WebStatusStage.SEARCHING_SOURCES, 1),
    )
    chat_step_ids = {
        "receiving_media",
        "staging_documents",
        "backend_wait",
        "delivering_result",
    }
    ids = [step.step_id for projection in projections for step in projection.ordered_steps]

    assert not chat_step_ids.intersection(ids)
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    ("builder", "stage"),
    [
        (build_files_operation_progress, FilesStatusStage.COMPLETE),
        (build_archive_operation_progress, ArchiveStatusStage.COMPLETE),
        (build_web_operation_progress, WebStatusStage.COMPLETE),
    ],
)
def test_complete_journey_has_no_running_focus_and_no_eta(builder, stage) -> None:
    projection = builder(stage, 7)
    text = render_operation_progress(projection)

    assert projection.terminal is True
    assert all(step.state is OperationStepState.COMPLETED for step in projection.ordered_steps)
    assert all(step.percentage == 100 for step in projection.ordered_steps)
    assert sum(step.state is OperationStepState.RUNNING for step in projection.ordered_steps) == 0
    assert "ETA" not in text
    assert "Тайм-аут" not in text


def test_backend_wait_does_not_mint_web_or_archive_stages() -> None:
    text = render_interactive_turn_status(
        TelegramStatusStage.BACKEND_WAIT,
        12,
        web_source_total=3,
        generated_file_total=2,
        generated_archive_total=2,
    )

    assert "ядро обрабатывает запрос" in text
    assert "Выполняю задачу" in text
    assert "ищу источники" not in text
    assert "Исследую вопрос" not in text
    assert "Собираю архив" not in text


def test_delivering_uses_observed_web_source_counts() -> None:
    text = render_interactive_turn_status(
        TelegramStatusStage.DELIVERING_RESULT,
        20,
        web_source_total=3,
    )

    assert text.startswith("⏳ Исследую вопрос")
    assert "формирую ответ" in text
    assert "ядро обрабатывает запрос" not in text
    assert all(token in {"0%", "100%"} for token in re.findall(r"\d+%", text))


def test_delivering_uses_observed_generated_archive_counts() -> None:
    text = render_interactive_turn_status(
        TelegramStatusStage.DELIVERING_RESULT,
        8,
        generated_file_total=2,
        generated_archive_total=2,
    )

    assert text.startswith("⏳ Собираю архив")
    assert "отправляю готовый архив" in text
    assert "ядро обрабатывает запрос" not in text


def test_inbound_album_keeps_document_mode_even_with_web_sources() -> None:
    text = render_interactive_turn_status(
        TelegramStatusStage.DELIVERING_RESULT,
        8,
        item_total=2,
        received_items=2,
        staged_items=2,
        web_source_total=3,
        generated_file_total=1,
        generated_archive_total=1,
    )

    assert text.startswith("⏳ Обрабатываю файлы")
    assert "Исследую вопрос" not in text
    assert "Собираю архив" not in text
