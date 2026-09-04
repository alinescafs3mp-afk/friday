"""Pure Engineer progress and result-carrier policy contracts.

This module is deliberately a seam, not a Telegram or Engineer integration.
It turns already-authoritative Engineer observations into the shared
``OperationProgressProjectionV1`` and chooses a truthful final carrier.  The
shared renderer is imported directly so there is one presentation engine for
all interactive operations.

No function here opens a path, sends a message, archives bytes, or mutates a
store.  In particular, internal command evidence is not a user result unless
the caller explicitly opts it in.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

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


class EngineerResultCarrierKind(StrEnum):
    """The only final carrier shapes permitted for an Engineer result."""

    TEXT = "text"
    FILE = "file"
    ARCHIVE = "archive"
    AUTO = "auto"


ResultCarrierKind = EngineerResultCarrierKind
EngineerResultCarrier = EngineerResultCarrierKind


class EngineerResultPolicyError(ValueError):
    """A result carrier would be empty, misleading, or internally scoped."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "engineer_result_policy_invalid")
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class EngineerResultFile:
    """A path-only result descriptor; bytes stay with the effect owner."""

    relative_path: str
    mime_type: str = "application/octet-stream"
    size_bytes: int | None = None
    internal: bool = False


@dataclass(frozen=True, slots=True)
class EngineerResultCarrierPlan:
    carrier: EngineerResultCarrierKind
    files: tuple[EngineerResultFile, ...]
    reason: str = ""

    @property
    def kind(self) -> EngineerResultCarrierKind:
        return self.carrier

    @property
    def result_carrier(self) -> EngineerResultCarrierKind:
        return self.carrier

    @property
    def is_archive(self) -> bool:
        return self.carrier is EngineerResultCarrierKind.ARCHIVE


_INTERNAL_COMPONENTS = frozenset(
    {
        ".cache",
        "cache",
        "caches",
        "log",
        "logs",
        "manifest",
        "manifests",
        "receipt",
        "receipts",
        "tmp",
        "temp",
        "__pycache__",
    }
)
_INTERNAL_BASENAME_RE = re.compile(
    r"(?:^|[-_.])(cache|caches|log|logs|manifest|manifests|receipt|receipts|tmp|stdout|stderr)(?:[-_.]|$)"
)
_ABSOLUTE_RESULT_PATH_RE = re.compile(r"^(?:/|[A-Za-z]:[\\/])")


def _validate_result_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise EngineerResultPolicyError("result_path_invalid")
    if _ABSOLUTE_RESULT_PATH_RE.match(value) or "\\" in value or "\x00" in value:
        raise EngineerResultPolicyError("result_path_invalid")
    parts = value.split("/")
    if any(
        not part
        or part in {".", ".."}
        or any(ord(character) < 32 or ord(character) == 127 for character in part)
        for part in parts
    ):
        raise EngineerResultPolicyError("result_path_invalid")
    return value


def _path_is_internal(path: str) -> bool:
    parts = tuple(part.casefold() for part in path.split("/"))
    if any(part in _INTERNAL_COMPONENTS for part in parts[:-1]):
        return True
    basename = parts[-1]
    if basename in {"receipt", "receipt.json", "manifest", "manifest.json", "stdout", "stderr"}:
        return True
    return _INTERNAL_BASENAME_RE.search(basename) is not None


def _normalise_result_file(value: object) -> EngineerResultFile:
    if isinstance(value, EngineerResultFile):
        path = _validate_result_path(value.relative_path)
        mime_type = value.mime_type
        size_bytes = value.size_bytes
        explicit_internal = value.internal
    elif isinstance(value, str):
        path = _validate_result_path(value)
        mime_type = "application/octet-stream"
        size_bytes = None
        explicit_internal = False
    elif isinstance(value, Mapping):
        path_value = value.get("relative_path", value.get("filename", value.get("path")))
        path = _validate_result_path(path_value)
        mime_type = value.get("mime_type", "application/octet-stream")
        size_bytes = value.get("size_bytes")
        explicit_internal = value.get("internal", False)
        if not isinstance(explicit_internal, bool):
            raise EngineerResultPolicyError("result_internal_flag_invalid")
    else:
        raise EngineerResultPolicyError("result_file_invalid")
    if (
        not isinstance(mime_type, str)
        or not mime_type.strip()
        or any(ord(character) < 32 for character in mime_type)
    ):
        raise EngineerResultPolicyError("result_mime_type_invalid")
    if size_bytes is not None and (
        isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0
    ):
        raise EngineerResultPolicyError("result_size_invalid")
    return EngineerResultFile(
        relative_path=path,
        mime_type=mime_type,
        size_bytes=size_bytes,
        internal=bool(explicit_internal) or _path_is_internal(path),
    )


def _normalise_result_files(
    files: Iterable[EngineerResultFile | str | Mapping[str, Any]]
    | EngineerResultFile
    | str
    | Mapping[str, Any]
    | None,
) -> tuple[EngineerResultFile, ...]:
    if files is None:
        raw_files: tuple[object, ...] = ()
    elif isinstance(files, (EngineerResultFile, str, Mapping)):
        raw_files = (files,)
    else:
        try:
            raw_files = tuple(files)
        except TypeError as exc:
            raise EngineerResultPolicyError("result_files_invalid") from exc
    normalised = tuple(_normalise_result_file(item) for item in raw_files)
    seen: set[str] = set()
    for item in normalised:
        portable = item.relative_path.casefold()
        if portable in seen:
            raise EngineerResultPolicyError("result_file_duplicate")
        seen.add(portable)
    return tuple(sorted(normalised, key=lambda item: item.relative_path.encode("utf-8")))


def is_internal_result_file(value: EngineerResultFile | str | Mapping[str, Any]) -> bool:
    """Return whether a descriptor names command-internal evidence."""

    item = _normalise_result_file(value)
    return item.internal or _path_is_internal(item.relative_path)


def select_user_result_files(
    files: Iterable[EngineerResultFile | str | Mapping[str, Any]]
    | EngineerResultFile
    | str
    | Mapping[str, Any]
    | None,
    *,
    include_internal: bool = False,
) -> tuple[EngineerResultFile, ...]:
    """Keep user outputs and hide receipts/logs/tmp/evidence by default."""

    if not isinstance(include_internal, bool):
        raise EngineerResultPolicyError("include_internal_invalid")
    normalised = _normalise_result_files(files)
    if include_internal:
        return normalised
    return tuple(
        item for item in normalised if not item.internal and not _path_is_internal(item.relative_path)
    )


filter_user_result_files = select_user_result_files


def _carrier(value: EngineerResultCarrierKind | str) -> EngineerResultCarrierKind:
    if isinstance(value, EngineerResultCarrierKind):
        return value
    try:
        return EngineerResultCarrierKind(str(value))
    except ValueError as exc:
        raise EngineerResultPolicyError("result_carrier_invalid") from exc


def validate_engineer_result_carrier(
    carrier: EngineerResultCarrierKind | str,
    files: Iterable[EngineerResultFile | str | Mapping[str, Any]]
    | EngineerResultFile
    | str
    | Mapping[str, Any]
    | None = None,
    *,
    include_internal: bool = False,
) -> EngineerResultCarrierPlan:
    """Validate one explicit carrier, refusing empty and one-file ZIPs."""

    selected = select_user_result_files(files, include_internal=include_internal)
    requested = _carrier(carrier)
    if requested is EngineerResultCarrierKind.AUTO:
        return select_engineer_result_carrier(selected, include_internal=True)
    if requested is EngineerResultCarrierKind.TEXT:
        if selected:
            raise EngineerResultPolicyError("text_carrier_drops_files")
        return EngineerResultCarrierPlan(requested, (), "no_user_output")
    if requested is EngineerResultCarrierKind.FILE:
        if len(selected) != 1:
            raise EngineerResultPolicyError("file_carrier_requires_one_file")
        return EngineerResultCarrierPlan(requested, selected, "one_user_output")
    if not selected:
        raise EngineerResultPolicyError("empty_archive")
    if len(selected) == 1 and not selected[0].internal:
        raise EngineerResultPolicyError("single_ordinary_file_archive_forbidden")
    return EngineerResultCarrierPlan(requested, selected, "multiple_user_outputs")


def select_engineer_result_carrier(
    files: Iterable[EngineerResultFile | str | Mapping[str, Any]]
    | EngineerResultFile
    | str
    | Mapping[str, Any]
    | None,
    *,
    requested: EngineerResultCarrierKind | str | None = None,
    archive_requested: bool = False,
    include_internal: bool = False,
) -> EngineerResultCarrierPlan:
    """Select a non-empty final carrier without wrapping one ordinary file."""

    if not isinstance(archive_requested, bool):
        raise EngineerResultPolicyError("archive_requested_invalid")
    selected = select_user_result_files(files, include_internal=include_internal)
    requested_kind = EngineerResultCarrierKind.AUTO if requested is None else _carrier(requested)
    if archive_requested:
        if requested_kind not in {EngineerResultCarrierKind.AUTO, EngineerResultCarrierKind.ARCHIVE}:
            raise EngineerResultPolicyError("carrier_request_conflict")
        requested_kind = EngineerResultCarrierKind.ARCHIVE
    if requested_kind is EngineerResultCarrierKind.TEXT:
        return validate_engineer_result_carrier(requested_kind, selected, include_internal=True)
    if requested_kind is EngineerResultCarrierKind.FILE:
        return validate_engineer_result_carrier(requested_kind, selected, include_internal=True)
    if requested_kind is EngineerResultCarrierKind.ARCHIVE:
        if not selected:
            return EngineerResultCarrierPlan(
                EngineerResultCarrierKind.TEXT,
                (),
                "empty_archive_replaced_by_text",
            )
        if len(selected) == 1:
            return EngineerResultCarrierPlan(
                EngineerResultCarrierKind.FILE,
                selected,
                "single_ordinary_file_sent_directly",
            )
        return validate_engineer_result_carrier(
            EngineerResultCarrierKind.ARCHIVE,
            selected,
            include_internal=True,
        )
    if not selected:
        return EngineerResultCarrierPlan(EngineerResultCarrierKind.TEXT, (), "no_user_output")
    if len(selected) == 1:
        return EngineerResultCarrierPlan(EngineerResultCarrierKind.FILE, selected, "one_user_output")
    return validate_engineer_result_carrier(
        EngineerResultCarrierKind.ARCHIVE,
        selected,
        include_internal=True,
    )


plan_engineer_result_carrier = select_engineer_result_carrier
choose_engineer_result_carrier = select_engineer_result_carrier
visible_result_files = select_user_result_files
validate_result_carrier = validate_engineer_result_carrier


def can_build_engineer_archive(
    files: Iterable[EngineerResultFile | str | Mapping[str, Any]]
    | EngineerResultFile
    | str
    | Mapping[str, Any]
    | None,
    *,
    include_internal: bool = False,
) -> bool:
    """Return whether an archive is a truthful final carrier for ``files``."""

    try:
        return validate_engineer_result_carrier(
            EngineerResultCarrierKind.ARCHIVE,
            files,
            include_internal=include_internal,
        ).is_archive
    except EngineerResultPolicyError:
        return False


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
