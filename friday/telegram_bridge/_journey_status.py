"""Truthful progress projections for non-chat Telegram journeys.

The bridge owns three related journeys that are not ordinary ``/chat``:
receiving and processing files, assembling an archive, and researching the
web.  This module only translates bridge-observable counters and closed stage
facts into the shared operation-progress contract.  It deliberately does not
send messages or execute work.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from friday.orchestration.mixed_journey_progress import MixedStatusStage, build_mixed_operation_progress
from friday.orchestration.operation_progress import (
    OperationMode,
    OperationProgressProjection,
    OperationStepState,
    ResultDeliveryState,
    build_operation_progress,
    render_operation_progress,
)

OperationProgressProjectionV1 = OperationProgressProjection


class FilesStatusStage(StrEnum):
    """Closed stages observable while a file journey is running."""

    RECEIVING_FILES = "receiving_files"
    PROCESSING_DOCUMENTS = "processing_documents"
    DELIVERING_RESULT = "delivering_result"
    COMPLETE = "complete"
    STOPPED = "stopped"


class ArchiveStatusStage(StrEnum):
    """Closed stages observable while an archive is assembled."""

    SELECTING_FILES = "selecting_files"
    BUILDING_ARCHIVE = "building_archive"
    DELIVERING_RESULT = "delivering_result"
    COMPLETE = "complete"
    STOPPED = "stopped"


class WebStatusStage(StrEnum):
    """Closed stages observable while public web sources are researched."""

    SEARCHING_SOURCES = "searching_sources"
    READING_SOURCES = "reading_sources"
    FORMULATING_ANSWER = "formulating_answer"
    COMPLETE = "complete"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class _JourneyStep:
    step_id: str
    safe_label: str
    evidence_class: str


@dataclass(frozen=True, slots=True)
class _MeasuredUnits:
    total: int | None
    completed: int | None

    @property
    def available(self) -> bool:
        return self.total is not None and self.total > 0


StageT = TypeVar("StageT", bound=StrEnum)


def _coerce_stage(stage: StageT | str, stage_type: type[StageT]) -> StageT:
    if isinstance(stage, stage_type):
        return stage
    return stage_type(str(stage))


def _units(total: int | None, completed: int | None) -> _MeasuredUnits:
    if total is None or int(total) <= 0:
        return _MeasuredUnits(None, None)
    bounded_total = int(total)
    bounded_completed = min(max(0, int(completed or 0)), bounded_total)
    return _MeasuredUnits(bounded_total, bounded_completed)


def _state_for(
    index: int,
    current_index: int,
    *,
    terminal: bool,
    stopped: bool,
) -> OperationStepState:
    if terminal and not stopped:
        return OperationStepState.COMPLETED
    if stopped:
        return OperationStepState.COMPLETED if index < current_index else OperationStepState.CANCELLED
    if index < current_index:
        return OperationStepState.COMPLETED
    if index == current_index:
        return OperationStepState.RUNNING
    return OperationStepState.PENDING


def _payload(
    step: _JourneyStep,
    state: OperationStepState,
    measured: _MeasuredUnits,
) -> dict[str, Any]:
    completed: int | None = measured.completed if measured.available else None
    total: int | None = measured.total if measured.available else None
    if state is OperationStepState.COMPLETED:
        if total is not None:
            completed = total
        percentage: int | None = 100
    elif state is OperationStepState.PENDING:
        if total is not None:
            completed = 0
        percentage = 0
    else:
        # Running, cancelled and failed stages have no fabricated percentage.
        percentage = None
    return {
        "step_id": step.step_id,
        "safe_label": step.safe_label,
        "state": str(state),
        "completed_units": completed,
        "total_units": total,
        "percentage": percentage,
        "evidence_class": step.evidence_class,
    }


def _stopped_index(measurements: tuple[_MeasuredUnits, ...], step_count: int) -> int:
    """Choose the first unproven step when a journey is stopped."""

    for index, measured in enumerate(measurements):
        if measured.available and measured.completed == measured.total:
            continue
        return index
    return max(0, step_count - 1)


def _build_journey_operation_progress(
    stage: StageT | str,
    elapsed_sec: float,
    *,
    stage_type: type[StageT],
    current_steps: dict[StageT, int],
    steps: tuple[_JourneyStep, ...],
    measurements: tuple[_MeasuredUnits, ...],
    mode: OperationMode,
    title: str,
    operation_id: str,
    authenticated_turn_id: str,
    revision: int,
) -> OperationProgressProjection:
    current_stage = _coerce_stage(stage, stage_type)
    terminal = current_stage in {
        stage_type("complete"),
        stage_type("stopped"),
    }
    stopped = current_stage == stage_type("stopped")
    if stopped:
        current_index = _stopped_index(measurements, len(steps))
    elif current_stage == stage_type("complete"):
        current_index = len(steps) - 1
    else:
        current_index = current_steps[current_stage]
    ordered_steps = tuple(
        _payload(
            step,
            _state_for(
                index,
                current_index,
                terminal=terminal,
                stopped=stopped,
            ),
            measured,
        )
        for index, (step, measured) in enumerate(zip(steps, measurements, strict=True))
    )
    if terminal:
        active_step_id = steps[current_index].step_id
        delivery = ResultDeliveryState.UNCERTAIN if stopped else ResultDeliveryState.CONFIRMED
    else:
        active_step_id = steps[current_index].step_id
        delivery = (
            ResultDeliveryState.IN_FLIGHT
            if current_index == len(steps) - 1
            else ResultDeliveryState.NOT_STARTED
        )
    return build_operation_progress(
        {
            "operation_id": operation_id,
            "authenticated_turn_id": authenticated_turn_id,
            "revision": max(1, int(revision)),
            "terminal": terminal,
            "mode": str(mode),
            "title": title,
            "ordered_steps": ordered_steps,
            "active_step_id": active_step_id,
            "elapsed_sec": max(0, int(elapsed_sec)),
            "hard_deadline_remaining_sec": None,
            "result_delivery_state": str(delivery),
            "plan_generation": 1,
        }
    )


_FILE_STEPS = (
    _JourneyStep("file_intake", "получаю файлы", "files"),
    _JourneyStep("document_processing", "обрабатываю документы", "files"),
    _JourneyStep("file_result", "отправляю готовый результат", "none"),
)
_FILE_CURRENT_STEPS = {
    FilesStatusStage.RECEIVING_FILES: 0,
    FilesStatusStage.PROCESSING_DOCUMENTS: 1,
    FilesStatusStage.DELIVERING_RESULT: 2,
}


def build_files_operation_progress(
    stage: FilesStatusStage | str,
    elapsed_sec: float,
    *,
    file_total: int = 0,
    received_files: int = 0,
    processed_files: int = 0,
    operation_id: str = "document:files",
    authenticated_turn_id: str = "document:files",
    revision: int = 1,
) -> OperationProgressProjection:
    """Build a document-mode projection from file counters and a closed stage."""

    return _build_journey_operation_progress(
        stage,
        elapsed_sec,
        stage_type=FilesStatusStage,
        current_steps=_FILE_CURRENT_STEPS,
        steps=_FILE_STEPS,
        measurements=(
            _units(file_total, received_files),
            _units(file_total, processed_files),
            _MeasuredUnits(None, None),
        ),
        mode=OperationMode.DOCUMENT,
        title="Обрабатываю файлы",
        operation_id=operation_id,
        authenticated_turn_id=authenticated_turn_id,
        revision=revision,
    )


_ARCHIVE_STEPS = (
    _JourneyStep("archive_selection", "подбираю файлы для архива", "files"),
    _JourneyStep("archive_building", "собираю архив", "files"),
    _JourneyStep("archive_result", "отправляю готовый архив", "none"),
)
_ARCHIVE_CURRENT_STEPS = {
    ArchiveStatusStage.SELECTING_FILES: 0,
    ArchiveStatusStage.BUILDING_ARCHIVE: 1,
    ArchiveStatusStage.DELIVERING_RESULT: 2,
}


def build_archive_operation_progress(
    stage: ArchiveStatusStage | str,
    elapsed_sec: float,
    *,
    file_total: int = 0,
    selected_files: int = 0,
    archived_files: int = 0,
    operation_id: str = "document:archive",
    authenticated_turn_id: str = "document:archive",
    revision: int = 1,
) -> OperationProgressProjection:
    """Build a document-mode projection from archive counters and a closed stage."""

    return _build_journey_operation_progress(
        stage,
        elapsed_sec,
        stage_type=ArchiveStatusStage,
        current_steps=_ARCHIVE_CURRENT_STEPS,
        steps=_ARCHIVE_STEPS,
        measurements=(
            _units(file_total, selected_files),
            _units(file_total, archived_files),
            _MeasuredUnits(None, None),
        ),
        mode=OperationMode.DOCUMENT,
        title="Собираю архив",
        operation_id=operation_id,
        authenticated_turn_id=authenticated_turn_id,
        revision=revision,
    )


_WEB_STEPS = (
    _JourneyStep("web_discovery", "ищу источники в интернете", "sources"),
    _JourneyStep("source_review", "проверяю найденные источники", "sources"),
    _JourneyStep("answer_composition", "формирую ответ", "none"),
)
_WEB_CURRENT_STEPS = {
    WebStatusStage.SEARCHING_SOURCES: 0,
    WebStatusStage.READING_SOURCES: 1,
    WebStatusStage.FORMULATING_ANSWER: 2,
}


def build_web_operation_progress(
    stage: WebStatusStage | str,
    elapsed_sec: float,
    *,
    source_total: int = 0,
    discovered_sources: int = 0,
    reviewed_sources: int = 0,
    operation_id: str = "research:web",
    authenticated_turn_id: str = "research:web",
    revision: int = 1,
) -> OperationProgressProjection:
    """Build a research-mode projection from source counters and a closed stage."""

    return _build_journey_operation_progress(
        stage,
        elapsed_sec,
        stage_type=WebStatusStage,
        current_steps=_WEB_CURRENT_STEPS,
        steps=_WEB_STEPS,
        measurements=(
            _units(source_total, discovered_sources),
            _units(source_total, reviewed_sources),
            _MeasuredUnits(None, None),
        ),
        mode=OperationMode.RESEARCH,
        title="Исследую вопрос",
        operation_id=operation_id,
        authenticated_turn_id=authenticated_turn_id,
        revision=revision,
    )


__all__ = [
    "ArchiveStatusStage",
    "FilesStatusStage",
    "MixedStatusStage",
    "OperationProgressProjectionV1",
    "WebStatusStage",
    "build_archive_operation_progress",
    "build_files_operation_progress",
    "build_mixed_operation_progress",
    "build_web_operation_progress",
    "render_operation_progress",
]
