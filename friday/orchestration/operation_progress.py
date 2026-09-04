"""Read-only operation-progress projection and code-owned Russian renderer.

This module owns presentation facts only.  It is not an effect owner, task
authority, scheduler, or WorkGraph.  Callers derive the projection from
existing authoritative stores and the current turn contract.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

OPERATION_PROGRESS_SCHEMA = "friday.operation-progress-projection.v1"
MAX_STATUS_CHARS = 4096
MAX_STEPS = 24
MAX_LABEL_CHARS = 80
MAX_TITLE_CHARS = 80
MAX_OPERATION_ID_CHARS = 128
MAX_TURN_ID_CHARS = 128
MAX_REVISION = 2_147_483_647
MAX_UNITS = 1_000_000
MAX_ELAPSED_SEC = 7 * 24 * 60 * 60

_OPERATION_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_TURN_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_STEP_ID_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_EVIDENCE_CLASS_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:^|[\s«\"'(])(?:~(?:/|$)|(?:/home|/etc|/var|/usr|/tmp|/root|/opt)/|"
    r"[A-Za-z]:\\)"
)
_TRAVERSAL_RE = re.compile(r"(?:^|[\s/\\])\.\.(?:[/\\]|$)")
_URL_RE = re.compile(r"://")
_SECRETISH_RE = re.compile(r"(?i)(?:api[_-]?key|password|secret|token|bearer|authorization)\s*[:=]")
_UNIT_NOUNS = {
    "files": "файлов",
    "sources": "источников",
    "tests": "тестов",
    "hosts": "хостов",
    "tasks": "задач",
    "stages": "этапов",
}


class OperationProgressError(ValueError):
    """A value is outside the closed operation-progress contract."""


class OperationMode(StrEnum):
    CHAT = "chat"
    ENGINEER = "engineer"
    CODING = "coding"
    RESEARCH = "research"
    DOCUMENT = "document"
    MIXED = "mixed"


class OperationStepState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ResultDeliveryState(StrEnum):
    NOT_STARTED = "not_started"
    IN_FLIGHT = "in_flight"
    CONFIRMED = "confirmed"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _require(condition: object, code: str) -> None:
    if not condition:
        raise OperationProgressError(code)


def _safe_text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OperationProgressError(f"{field}_invalid")
    if len(value) > maximum:
        raise OperationProgressError(f"{field}_too_long")
    if _contains_control(value):
        raise OperationProgressError(f"{field}_control")
    if _ABSOLUTE_PATH_RE.search(value) is not None or _TRAVERSAL_RE.search(value) is not None:
        raise OperationProgressError(f"{field}_path")
    if _URL_RE.search(value) is not None:
        raise OperationProgressError(f"{field}_url")
    if _SECRETISH_RE.search(value) is not None:
        raise OperationProgressError(f"{field}_secret")
    return value


def _optional_int(value: object, *, field: str, maximum: int) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise OperationProgressError(f"{field}_invalid")
    if not 0 <= value <= maximum:
        raise OperationProgressError(f"{field}_out_of_range")
    return value


def _elapsed_label(elapsed_sec: int) -> str:
    minutes, remainder = divmod(max(0, elapsed_sec), 60)
    if not minutes:
        return f"{remainder} с"
    if not remainder:
        return f"{minutes} мин"
    return f"{minutes} мин {remainder} с"


def _unit_suffix(evidence_class: str, completed: int, total: int) -> str:
    noun = _UNIT_NOUNS.get(evidence_class)
    counted = f"{completed} из {total}"
    return f"{counted} {noun}" if noun else counted


@dataclass(frozen=True, slots=True)
class OperationStep:
    step_id: str
    safe_label: str
    state: OperationStepState
    completed_units: int | None
    total_units: int | None
    percentage: int | None
    evidence_class: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "safe_label": self.safe_label,
            "state": str(self.state),
            "completed_units": self.completed_units,
            "total_units": self.total_units,
            "percentage": self.percentage,
            "evidence_class": self.evidence_class,
        }


@dataclass(frozen=True, slots=True)
class OperationProgressProjection:
    schema: str
    operation_id: str
    authenticated_turn_id: str
    revision: int
    terminal: bool
    mode: OperationMode
    title: str
    ordered_steps: tuple[OperationStep, ...]
    active_step_id: str | None
    elapsed_sec: int
    hard_deadline_remaining_sec: int | None
    result_delivery_state: ResultDeliveryState
    plan_generation: int

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "operation_id": self.operation_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "revision": self.revision,
            "terminal": self.terminal,
            "mode": str(self.mode),
            "title": self.title,
            "ordered_steps": [step.to_mapping() for step in self.ordered_steps],
            "active_step_id": self.active_step_id,
            "elapsed_sec": self.elapsed_sec,
            "hard_deadline_remaining_sec": self.hard_deadline_remaining_sec,
            "result_delivery_state": str(self.result_delivery_state),
            "plan_generation": self.plan_generation,
        }


def _parse_step(raw: Mapping[str, Any]) -> OperationStep:
    _require(isinstance(raw, Mapping), "step_invalid")
    step_id = raw.get("step_id")
    if not isinstance(step_id, str) or _STEP_ID_RE.fullmatch(step_id) is None:
        raise OperationProgressError("step_id_invalid")
    try:
        state = OperationStepState(str(raw.get("state") or ""))
    except ValueError as exc:
        raise OperationProgressError("step_state_invalid") from exc
    evidence_class = raw.get("evidence_class")
    if not isinstance(evidence_class, str) or _EVIDENCE_CLASS_RE.fullmatch(evidence_class) is None:
        raise OperationProgressError("evidence_class_invalid")
    completed = _optional_int(raw.get("completed_units"), field="completed_units", maximum=MAX_UNITS)
    total = _optional_int(raw.get("total_units"), field="total_units", maximum=MAX_UNITS)
    percentage = _optional_int(raw.get("percentage"), field="percentage", maximum=100)
    if total is None:
        _require(completed is None, "units_incomplete")
        if state is OperationStepState.COMPLETED:
            _require(percentage in (None, 100), "percentage_unmeasured")
            percentage = 100
        elif state is OperationStepState.PENDING:
            _require(percentage in (None, 0), "percentage_unmeasured")
            percentage = 0
        else:
            _require(percentage is None, "percentage_unmeasured")
    else:
        _require(total >= 1, "total_units_invalid")
        if completed is None:
            raise OperationProgressError("completed_units_missing")
        _require(completed <= total, "completed_units_out_of_range")
        derived = (completed * 100) // total
        if state is OperationStepState.COMPLETED:
            _require(completed == total, "completed_units_mismatch")
            _require(percentage in (None, 100), "percentage_unmeasured")
            percentage = 100
        elif state is OperationStepState.PENDING:
            _require(completed == 0, "pending_units_mismatch")
            _require(percentage in (None, 0), "percentage_unmeasured")
            percentage = 0
        elif percentage is not None:
            _require(percentage == derived, "percentage_unmeasured")
    return OperationStep(
        step_id=step_id,
        safe_label=_safe_text(raw.get("safe_label"), field="safe_label", maximum=MAX_LABEL_CHARS),
        state=state,
        completed_units=completed,
        total_units=total,
        percentage=percentage,
        evidence_class=evidence_class,
    )


def build_operation_progress(raw: Mapping[str, Any]) -> OperationProgressProjection:
    """Admit one closed progress projection.  Unknown keys are ignored."""

    _require(isinstance(raw, Mapping), "projection_invalid")
    schema = raw.get("schema", OPERATION_PROGRESS_SCHEMA)
    _require(schema == OPERATION_PROGRESS_SCHEMA, "schema_invalid")
    operation_id = raw.get("operation_id")
    if not isinstance(operation_id, str) or _OPERATION_ID_RE.fullmatch(operation_id) is None:
        raise OperationProgressError("operation_id_invalid")
    turn_id = raw.get("authenticated_turn_id")
    if not isinstance(turn_id, str) or _TURN_ID_RE.fullmatch(turn_id) is None:
        raise OperationProgressError("turn_id_invalid")
    revision = raw.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise OperationProgressError("revision_invalid")
    if not 1 <= revision <= MAX_REVISION:
        raise OperationProgressError("revision_out_of_range")
    terminal = raw.get("terminal")
    if not isinstance(terminal, bool):
        raise OperationProgressError("terminal_invalid")
    try:
        mode = OperationMode(str(raw.get("mode") or ""))
    except ValueError as exc:
        raise OperationProgressError("mode_invalid") from exc
    try:
        delivery = ResultDeliveryState(str(raw.get("result_delivery_state") or ""))
    except ValueError as exc:
        raise OperationProgressError("delivery_state_invalid") from exc
    elapsed = raw.get("elapsed_sec")
    if not isinstance(elapsed, int) or isinstance(elapsed, bool):
        raise OperationProgressError("elapsed_invalid")
    if not 0 <= elapsed <= MAX_ELAPSED_SEC:
        raise OperationProgressError("elapsed_out_of_range")
    remaining = _optional_int(
        raw.get("hard_deadline_remaining_sec"),
        field="hard_deadline_remaining_sec",
        maximum=MAX_ELAPSED_SEC,
    )
    plan_generation = raw.get("plan_generation", 1)
    if not isinstance(plan_generation, int) or isinstance(plan_generation, bool):
        raise OperationProgressError("plan_generation_invalid")
    if not 1 <= plan_generation <= MAX_REVISION:
        raise OperationProgressError("plan_generation_out_of_range")
    steps_raw = raw.get("ordered_steps")
    if not isinstance(steps_raw, Sequence) or isinstance(steps_raw, (str, bytes)):
        raise OperationProgressError("steps_invalid")
    if not 1 <= len(steps_raw) <= MAX_STEPS:
        raise OperationProgressError("steps_count_invalid")
    steps = tuple(_parse_step(item) for item in steps_raw)
    seen: set[str] = set()
    running: list[str] = []
    for step in steps:
        _require(step.step_id not in seen, "step_id_duplicate")
        seen.add(step.step_id)
        if step.state is OperationStepState.RUNNING:
            running.append(step.step_id)
    _require(len(running) <= 1, "multiple_running_steps")
    active = raw.get("active_step_id")
    if active is None:
        _require(not running, "active_step_missing")
    else:
        _require(isinstance(active, str) and active in seen, "active_step_invalid")
        if running:
            _require(active == running[0], "active_step_mismatch")
        else:
            _require(terminal or steps[0].state is not OperationStepState.RUNNING, "active_step_mismatch")
    if terminal:
        _require(not running, "terminal_running_step")
        _require(
            delivery
            in {
                ResultDeliveryState.CONFIRMED,
                ResultDeliveryState.UNCERTAIN,
                ResultDeliveryState.FAILED,
            },
            "terminal_delivery_invalid",
        )
    else:
        _require(
            delivery in {ResultDeliveryState.NOT_STARTED, ResultDeliveryState.IN_FLIGHT},
            "running_delivery_invalid",
        )
    projection = OperationProgressProjection(
        schema=OPERATION_PROGRESS_SCHEMA,
        operation_id=operation_id,
        authenticated_turn_id=turn_id,
        revision=revision,
        terminal=terminal,
        mode=mode,
        title=_safe_text(raw.get("title"), field="title", maximum=MAX_TITLE_CHARS),
        ordered_steps=steps,
        active_step_id=active if isinstance(active, str) else None,
        elapsed_sec=elapsed,
        hard_deadline_remaining_sec=remaining,
        result_delivery_state=delivery,
        plan_generation=plan_generation,
    )
    rendered = render_operation_progress(projection)
    _require(1 <= len(rendered) <= MAX_STATUS_CHARS, "status_too_long")
    _require("\x00" not in rendered, "status_control")
    return projection


def _heading(projection: OperationProgressProjection) -> str:
    title = projection.title
    if projection.mode is OperationMode.CODING:
        return f"🛠 {title}"
    return f"⏳ {title}"


def _step_line(step: OperationStep) -> str:
    label = step.safe_label
    if step.state is OperationStepState.COMPLETED:
        return f"✅ {label} - 100%"
    if step.state is OperationStepState.PENDING:
        return f"▫️ {label} - 0%"
    if step.state is OperationStepState.BLOCKED:
        return f"⚠️ {label}"
    if step.state is OperationStepState.FAILED:
        return f"❌ {label}"
    if step.state is OperationStepState.CANCELLED:
        return f"⏹ {label}"
    detail = ""
    if step.total_units is not None and step.completed_units is not None:
        counted = _unit_suffix(step.evidence_class, step.completed_units, step.total_units)
        detail = f" - {counted}" if step.percentage is None else f" - {counted}, {step.percentage}%"
    elif step.percentage is not None:
        detail = f" - {step.percentage}%"
    return f"▶️ **{label}{detail}**"


def render_operation_progress(projection: OperationProgressProjection) -> str:
    """Render one Telegram-sized status from closed facts.  No ETA."""

    lines = [_heading(projection), ""]
    if projection.plan_generation > 1:
        lines.append("План уточнён")
        lines.append("")
    lines.extend(_step_line(step) for step in projection.ordered_steps)
    lines.append("")
    lines.append(f"Прошло: {_elapsed_label(projection.elapsed_sec)}")
    if projection.hard_deadline_remaining_sec is not None:
        lines.append(f"Тайм-аут: осталось {_elapsed_label(projection.hard_deadline_remaining_sec)}")
    if projection.result_delivery_state is ResultDeliveryState.UNCERTAIN:
        lines.append("⚠️ Доставка результата не подтверждена.")
    text = "\n".join(lines)
    if text.endswith("\n"):
        return text
    return text
