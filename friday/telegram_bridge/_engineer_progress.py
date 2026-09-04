"""Pure Engineer progress seam and re-exported result-carrier policy.

This module is deliberately a seam, not a Telegram or Engineer integration.
It turns already-authoritative Engineer observations into the shared
``OperationProgressProjectionV1``.  Final-carrier policy lives in
``friday.orchestration.engineer_result_carrier`` and is re-exported here so
existing imports keep working.  The shared renderer is imported directly so
there is one presentation engine for all interactive operations.

No function here opens a path, sends a message, archives bytes, or mutates a
store.  In particular, internal command evidence is not a user result unless
the caller explicitly opts it in.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from friday.orchestration.engineer_result_carrier import (
    EngineerResultCarrier,
    EngineerResultCarrierKind,
    EngineerResultCarrierPlan,
    EngineerResultFile,
    EngineerResultPolicyError,
    ResultCarrierKind,
    can_build_engineer_archive,
    choose_engineer_result_carrier,
    filter_user_result_files,
    is_internal_result_file,
    plan_engineer_result_carrier,
    select_engineer_result_carrier,
    select_user_result_files,
    validate_engineer_result_carrier,
    validate_result_carrier,
    visible_result_files,
)
from friday.orchestration.operation_progress import (
    OPERATION_PROGRESS_SCHEMA,
    OperationMode,
    OperationProgressError,
    OperationProgressProjection,
    OperationStepState,
    ResultDeliveryState,
    build_operation_progress,
    render_operation_progress,
)

OperationProgressProjectionV1 = OperationProgressProjection

ENGINEER_OPERATION_TITLE = "Engineer-задача"
ENGINEER_OPERATION_ID = "engineer:status"
ENGINEER_TURN_ID = "engineer:status"


class EngineerProgressStage(StrEnum):
    """Closed stage vocabulary admitted from an Engineer observation."""

    QUEUED = "queued"
    COMMAND_RUNNING = "command_running"
    DELIVERING_RESULT = "delivering_result"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"
    DELIVERY_UNCERTAIN = "delivery_uncertain"


EngineerOperationStage = EngineerProgressStage

_STAGE_ALIASES = {
    "planned": EngineerProgressStage.QUEUED,
    "admitted": EngineerProgressStage.QUEUED,
    "running": EngineerProgressStage.COMMAND_RUNNING,
    "complete": EngineerProgressStage.COMPLETED,
    "stopped": EngineerProgressStage.CANCELLED,
    "delivery_unknown": EngineerProgressStage.DELIVERY_UNCERTAIN,
    "delivery_uncertain": EngineerProgressStage.DELIVERY_UNCERTAIN,
}
_TERMINAL_STAGES = frozenset(
    {
        EngineerProgressStage.COMPLETED,
        EngineerProgressStage.FAILED,
        EngineerProgressStage.CANCELLED,
        EngineerProgressStage.TIMEOUT,
        EngineerProgressStage.UNKNOWN,
        EngineerProgressStage.DELIVERY_UNCERTAIN,
    }
)
_MAX_COUNTER = (1 << 63) - 1


def _stage(value: object) -> EngineerProgressStage:
    if isinstance(value, EngineerProgressStage):
        return value
    if not isinstance(value, str) or not value:
        raise OperationProgressError("engineer_stage_invalid")
    candidate = _STAGE_ALIASES.get(value, value)
    try:
        return EngineerProgressStage(candidate)
    except ValueError as exc:
        raise OperationProgressError("engineer_stage_invalid") from exc


def _number(value: object, *, field: str, maximum: int = _MAX_COUNTER) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        raise OperationProgressError(f"{field}_invalid")
    if value < 0 or value > maximum:
        raise OperationProgressError(f"{field}_out_of_range")
    return int(value)


def _mapping_value(raw: Mapping[str, Any], key: str, explicit: object, default: object) -> object:
    return explicit if explicit is not None else raw.get(key, default)


def _bytes_label(value: int) -> str:
    if value < 1024:
        return f"{value} Б"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} КиБ"
    if value < 1024 * 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} МиБ"
    return f"{value / (1024 * 1024 * 1024):.1f} ГиБ"


def _output_detail(stdout_bytes: int, stderr_bytes: int, output_activity: bool) -> str:
    if not output_activity:
        return "; текстового вывода ещё не было"
    return f"; stdout {_bytes_label(stdout_bytes)}, stderr {_bytes_label(stderr_bytes)}"


def _step(
    step_id: str,
    label: str,
    state: OperationStepState,
) -> dict[str, object]:
    return {
        "step_id": step_id,
        "safe_label": label,
        "state": str(state),
        "completed_units": None,
        "total_units": None,
        "percentage": None,
        "evidence_class": "none",
    }


def _engineer_steps(
    stage: EngineerProgressStage,
    *,
    stdout_bytes: int,
    stderr_bytes: int,
    output_activity: bool,
    delivery: ResultDeliveryState,
) -> tuple[list[dict[str, object]], str]:
    command_state = OperationStepState.PENDING
    command_label = "ожидает запуска"
    active = "command"

    if stage is EngineerProgressStage.COMMAND_RUNNING:
        command_state = OperationStepState.RUNNING
        command_label = "выполняется команда" + _output_detail(stdout_bytes, stderr_bytes, output_activity)
    elif stage in {
        EngineerProgressStage.DELIVERING_RESULT,
        EngineerProgressStage.COMPLETED,
        EngineerProgressStage.DELIVERY_UNCERTAIN,
    }:
        command_state = OperationStepState.COMPLETED
        command_label = "команда выполнена"
    elif stage is EngineerProgressStage.FAILED:
        command_state = OperationStepState.FAILED
        command_label = "команда завершилась с ошибкой"
    elif stage is EngineerProgressStage.CANCELLED:
        command_state = OperationStepState.CANCELLED
        command_label = "команда отменена"
    elif stage is EngineerProgressStage.TIMEOUT:
        command_state = OperationStepState.FAILED
        command_label = "команда остановлена по тайм-ауту"
    elif stage is EngineerProgressStage.UNKNOWN:
        command_state = OperationStepState.BLOCKED
        command_label = "состояние команды неизвестно"

    if delivery is ResultDeliveryState.CONFIRMED:
        delivery_state = OperationStepState.COMPLETED
        delivery_label = "результат доставлен"
    elif delivery is ResultDeliveryState.FAILED:
        delivery_state = OperationStepState.FAILED
        delivery_label = "результат не доставлен"
    elif delivery is ResultDeliveryState.UNCERTAIN:
        delivery_state = OperationStepState.BLOCKED
        delivery_label = "доставка результата не подтверждена"
    elif delivery is ResultDeliveryState.IN_FLIGHT:
        delivery_state = OperationStepState.RUNNING
        delivery_label = "отправляю результат"
    else:
        delivery_state = OperationStepState.PENDING
        delivery_label = "результат ожидает отправки"

    if delivery_state is OperationStepState.RUNNING or stage is EngineerProgressStage.DELIVERY_UNCERTAIN:
        active = "result_delivery"

    return (
        [
            _step("command", command_label, command_state),
            _step("result_delivery", delivery_label, delivery_state),
        ],
        active,
    )


def build_engineer_operation_progress(
    stage_or_update: EngineerProgressStage | str | Mapping[str, Any],
    elapsed_sec: int | float | None = None,
    *,
    operation_id: str | None = None,
    authenticated_turn_id: str | None = None,
    revision: int | None = None,
    timeout_sec: int | float | None = None,
    remaining_sec: int | float | None = None,
    stdout_bytes: int | None = None,
    stderr_bytes: int | None = None,
    output_activity: bool | None = None,
    terminal: bool | None = None,
    result_delivery_state: ResultDeliveryState | str | None = None,
    title: str | None = None,
    plan_generation: int | None = None,
) -> OperationProgressProjectionV1:
    """Build one Engineer projection from already-observed, safe facts.

    ``stage_or_update`` may be a closed stage or a mapping such as the
    ``status_update`` carried by the existing Engineer notification.  The
    mapping form is accepted to keep this module usable before live wiring is
    deliberately introduced.  Running work has one focus step and never
    receives a fabricated percentage or ETA.
    """

    raw: Mapping[str, Any]
    if isinstance(stage_or_update, Mapping):
        nested = stage_or_update.get("status_update", stage_or_update)
        if not isinstance(nested, Mapping):
            raise OperationProgressError("engineer_update_invalid")
        raw = nested
        stage_value: object = raw.get("stage", raw.get("status"))
    else:
        raw = {}
        stage_value = stage_or_update
    current_stage = _stage(stage_value)

    elapsed = _number(_mapping_value(raw, "elapsed_sec", elapsed_sec, 0), field="elapsed_sec")
    timeout = _number(_mapping_value(raw, "timeout_sec", timeout_sec, 0), field="timeout_sec")
    explicit_remaining = _mapping_value(raw, "remaining_sec", remaining_sec, None)
    if explicit_remaining is None:
        deadline_remaining = None if timeout == 0 else max(0, timeout - elapsed)
    else:
        deadline_remaining = _number(explicit_remaining, field="remaining_sec")
        expected = None if timeout == 0 else max(0, timeout - elapsed)
        if deadline_remaining != expected:
            raise OperationProgressError("remaining_sec_mismatch")

    stdout = _number(_mapping_value(raw, "stdout_bytes", stdout_bytes, 0), field="stdout_bytes")
    stderr = _number(_mapping_value(raw, "stderr_bytes", stderr_bytes, 0), field="stderr_bytes")
    activity_value = _mapping_value(raw, "output_activity", output_activity, None)
    if activity_value is None:
        activity = bool(stdout or stderr)
    elif not isinstance(activity_value, bool):
        raise OperationProgressError("output_activity_invalid")
    else:
        activity = activity_value

    terminal_value = _mapping_value(raw, "terminal", terminal, current_stage in _TERMINAL_STAGES)
    if not isinstance(terminal_value, bool):
        raise OperationProgressError("terminal_invalid")
    if terminal_value != (current_stage in _TERMINAL_STAGES):
        raise OperationProgressError("terminal_stage_mismatch")

    delivery_value = _mapping_value(raw, "result_delivery_state", result_delivery_state, None)
    if delivery_value is None:
        delivery = {
            EngineerProgressStage.COMPLETED: ResultDeliveryState.CONFIRMED,
            EngineerProgressStage.UNKNOWN: ResultDeliveryState.UNCERTAIN,
            EngineerProgressStage.DELIVERY_UNCERTAIN: ResultDeliveryState.UNCERTAIN,
            EngineerProgressStage.FAILED: ResultDeliveryState.FAILED,
            EngineerProgressStage.CANCELLED: ResultDeliveryState.FAILED,
            EngineerProgressStage.TIMEOUT: ResultDeliveryState.FAILED,
            EngineerProgressStage.DELIVERING_RESULT: ResultDeliveryState.IN_FLIGHT,
        }.get(current_stage, ResultDeliveryState.NOT_STARTED)
    else:
        try:
            delivery = ResultDeliveryState(str(delivery_value))
        except ValueError as exc:
            raise OperationProgressError("delivery_state_invalid") from exc
    if terminal_value:
        if delivery in {ResultDeliveryState.NOT_STARTED, ResultDeliveryState.IN_FLIGHT}:
            raise OperationProgressError("terminal_delivery_invalid")
    elif delivery not in {ResultDeliveryState.NOT_STARTED, ResultDeliveryState.IN_FLIGHT}:
        raise OperationProgressError("running_delivery_invalid")

    steps, active = _engineer_steps(
        current_stage,
        stdout_bytes=stdout,
        stderr_bytes=stderr,
        output_activity=activity,
        delivery=delivery,
    )
    # A caller may report a delivery outcome, but it cannot make a failed
    # command look successful through the shared renderer's heading rules.
    if (
        current_stage
        in {
            EngineerProgressStage.FAILED,
            EngineerProgressStage.CANCELLED,
            EngineerProgressStage.TIMEOUT,
        }
        and delivery is ResultDeliveryState.CONFIRMED
    ):
        raise OperationProgressError("failed_command_delivery_invalid")

    operation = _mapping_value(raw, "operation_id", operation_id, ENGINEER_OPERATION_ID)
    turn = _mapping_value(raw, "authenticated_turn_id", authenticated_turn_id, ENGINEER_TURN_ID)
    revision_value = _number(_mapping_value(raw, "revision", revision, 1), field="revision")
    generation_value = _number(
        _mapping_value(raw, "plan_generation", plan_generation, 1), field="plan_generation"
    )
    heading = _mapping_value(raw, "title", title, ENGINEER_OPERATION_TITLE)
    return build_operation_progress(
        {
            "schema": OPERATION_PROGRESS_SCHEMA,
            "operation_id": operation,
            "authenticated_turn_id": turn,
            "revision": revision_value,
            "terminal": terminal_value,
            "mode": str(OperationMode.ENGINEER),
            "title": heading,
            "ordered_steps": steps,
            "active_step_id": active,
            "elapsed_sec": elapsed,
            "hard_deadline_remaining_sec": deadline_remaining,
            "result_delivery_state": str(delivery),
            "plan_generation": generation_value,
        }
    )


def build_engineer_running_progress(
    elapsed_sec: int | float,
    **kwargs: Any,
) -> OperationProgressProjectionV1:
    """Convenience builder for the one running Engineer status stream."""

    return build_engineer_operation_progress(
        EngineerProgressStage.COMMAND_RUNNING,
        elapsed_sec,
        **kwargs,
    )


def build_engineer_terminal_progress(
    stage: EngineerProgressStage | str,
    elapsed_sec: int | float,
    **kwargs: Any,
) -> OperationProgressProjectionV1:
    """Convenience builder for a terminal Engineer status stream."""

    return build_engineer_operation_progress(stage, elapsed_sec, **kwargs)


build_engineer_progress = build_engineer_operation_progress


__all__ = [
    "ENGINEER_OPERATION_ID",
    "ENGINEER_OPERATION_TITLE",
    "ENGINEER_TURN_ID",
    "EngineerOperationStage",
    "EngineerProgressStage",
    "EngineerResultCarrier",
    "EngineerResultCarrierKind",
    "EngineerResultCarrierPlan",
    "EngineerResultFile",
    "EngineerResultPolicyError",
    "OperationMode",
    "OperationProgressError",
    "OperationProgressProjection",
    "OperationProgressProjectionV1",
    "OperationStepState",
    "ResultCarrierKind",
    "ResultDeliveryState",
    "build_engineer_operation_progress",
    "build_engineer_progress",
    "build_engineer_running_progress",
    "build_engineer_terminal_progress",
    "build_operation_progress",
    "can_build_engineer_archive",
    "choose_engineer_result_carrier",
    "filter_user_result_files",
    "is_internal_result_file",
    "plan_engineer_result_carrier",
    "render_operation_progress",
    "select_engineer_result_carrier",
    "select_user_result_files",
    "validate_result_carrier",
    "validate_engineer_result_carrier",
    "visible_result_files",
]
