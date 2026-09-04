"""Mixed-mode operation-progress builder for store-backed mixed journeys."""

from __future__ import annotations

from enum import StrEnum

from friday.orchestration.operation_progress import (
    OperationMode,
    OperationProgressProjection,
    OperationStepState,
    ResultDeliveryState,
    build_operation_progress,
)


class MixedStatusStage(StrEnum):
    """Closed stages observable while a mixed store-backed journey is composed."""

    GATHERING_FACTS = "gathering_facts"
    COMPOSING_RESULT = "composing_result"
    DELIVERING_RESULT = "delivering_result"
    COMPLETE = "complete"
    STOPPED = "stopped"


_STEPS = (
    ("mixed_intake", "собираю факты маршрута", "stages"),
    ("mixed_compose", "связываю органы в один результат", "stages"),
    ("mixed_result", "отправляю готовый результат", "none"),
)
_CURRENT = {
    MixedStatusStage.GATHERING_FACTS: 0,
    MixedStatusStage.COMPOSING_RESULT: 1,
    MixedStatusStage.DELIVERING_RESULT: 2,
}


def _units(total: int | None, completed: int | None) -> tuple[int | None, int | None]:
    if total is None or int(total) <= 0:
        return None, None
    bounded_total = int(total)
    return bounded_total, min(max(0, int(completed or 0)), bounded_total)


def _state(index: int, current_index: int, *, terminal: bool, stopped: bool) -> OperationStepState:
    if terminal and not stopped:
        return OperationStepState.COMPLETED
    if stopped:
        return OperationStepState.COMPLETED if index < current_index else OperationStepState.CANCELLED
    if index < current_index:
        return OperationStepState.COMPLETED
    if index == current_index:
        return OperationStepState.RUNNING
    return OperationStepState.PENDING


def _step(
    step_id: str,
    label: str,
    evidence: str,
    state: OperationStepState,
    total: int | None,
    completed: int | None,
) -> dict[str, object]:
    if state is OperationStepState.COMPLETED:
        if total is not None:
            completed = total
        percentage: int | None = 100
    elif state is OperationStepState.PENDING:
        if total is not None:
            completed = 0
        percentage = 0
    else:
        percentage = None
    return {
        "step_id": step_id,
        "safe_label": label,
        "state": str(state),
        "completed_units": completed if total is not None else None,
        "total_units": total,
        "percentage": percentage,
        "evidence_class": evidence,
    }


def build_mixed_operation_progress(
    stage: MixedStatusStage | str,
    elapsed_sec: float,
    *,
    organ_total: int = 0,
    gathered_organs: int = 0,
    composed_organs: int = 0,
    operation_id: str = "mixed:journey",
    authenticated_turn_id: str = "mixed:journey",
    revision: int = 1,
) -> OperationProgressProjection:
    """Build a mixed-mode projection from organ counters and a closed stage."""

    current = MixedStatusStage(str(stage))
    terminal = current in {MixedStatusStage.COMPLETE, MixedStatusStage.STOPPED}
    stopped = current is MixedStatusStage.STOPPED
    measurements = (
        _units(organ_total, gathered_organs),
        _units(organ_total, composed_organs),
        (None, None),
    )
    if stopped:
        current_index = 0
        for index, (total, completed) in enumerate(measurements):
            if total is not None and completed == total:
                continue
            current_index = index
            break
        else:
            current_index = len(_STEPS) - 1
    elif current is MixedStatusStage.COMPLETE:
        current_index = len(_STEPS) - 1
    else:
        current_index = _CURRENT[current]
    ordered = tuple(
        _step(
            step_id,
            label,
            evidence,
            _state(index, current_index, terminal=terminal, stopped=stopped),
            total,
            completed,
        )
        for index, ((step_id, label, evidence), (total, completed)) in enumerate(
            zip(_STEPS, measurements, strict=True)
        )
    )
    if terminal:
        delivery = ResultDeliveryState.UNCERTAIN if stopped else ResultDeliveryState.CONFIRMED
    else:
        delivery = (
            ResultDeliveryState.IN_FLIGHT
            if current_index == len(_STEPS) - 1
            else ResultDeliveryState.NOT_STARTED
        )
    return build_operation_progress(
        {
            "operation_id": operation_id,
            "authenticated_turn_id": authenticated_turn_id,
            "revision": max(1, int(revision)),
            "terminal": terminal,
            "mode": str(OperationMode.MIXED),
            "title": "Смешанный маршрут",
            "ordered_steps": ordered,
            "active_step_id": _STEPS[current_index][0],
            "elapsed_sec": max(0, int(elapsed_sec)),
            "hard_deadline_remaining_sec": None,
            "result_delivery_state": str(delivery),
            "plan_generation": 1,
        }
    )


__all__ = ["MixedStatusStage", "build_mixed_operation_progress"]
