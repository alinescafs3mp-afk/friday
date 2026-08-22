"""Small, deterministic Markdown task semantics for Obsidian notes."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, time

from .structured_notes import StructuredEdit, StructuredNoteError, append_section_item

_TASK = re.compile(
    r"^(?P<indent>[ \t]*)[-*+]\s+\[(?P<mark>[ xX])\]\s+"
    r"(?P<text>.*?)(?:\s+\^(?P<block>friday-task-[0-9a-f]{12}))?[ \t]*$"
)
_DUE = re.compile(r"(?:📅|due:)\s*(?P<date>\d{4}-\d{2}-\d{2})", re.IGNORECASE)
_TIME = re.compile(r"(?:⏰|time:)\s*(?P<time>\d{2}:\d{2})", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class MarkdownTask:
    text: str
    completed: bool
    due_date: date | None
    due_time: time | None
    block_id: str
    line_number: int


def render_dated_task(
    text: str,
    *,
    due_date: date,
    due_time: time | None = None,
    operation_id: str,
) -> str:
    """Render one standard checkbox with concrete local date/time and stable ID."""

    body = _one_line(text, "task text", 10_000)
    if not isinstance(due_date, date):
        raise StructuredNoteError("task due_date must be a date")
    if due_time is not None and not isinstance(due_time, time):
        raise StructuredNoteError("task due_time must be a time")
    operation = _one_line(operation_id, "operation_id", 200)
    block = hashlib.sha256(operation.encode("utf-8")).hexdigest()[:12]
    temporal = f"📅 {due_date.isoformat()}"
    if due_time is not None:
        temporal += f" ⏰ {due_time.strftime('%H:%M')}"
    return f"- [ ] {body} {temporal} ^friday-task-{block}"


def append_task(
    markdown: str,
    *,
    section: str,
    text: str,
    due_date: date,
    due_time: time | None,
    operation_id: str,
) -> StructuredEdit:
    return append_section_item(
        markdown,
        section,
        render_dated_task(
            text,
            due_date=due_date,
            due_time=due_time,
            operation_id=operation_id,
        ),
    )


def list_tasks(markdown: str, *, query: str = "", incomplete_only: bool = False) -> tuple[MarkdownTask, ...]:
    if not isinstance(markdown, str) or "\x00" in markdown or len(markdown) > 4 * 1024 * 1024:
        raise StructuredNoteError("Markdown is invalid or exceeds the task-index limit")
    folded_query = unicodedata.normalize("NFC", query).casefold().strip()
    terms = tuple(term for term in re.findall(r"\w+", folded_query) if len(term) > 1)
    tasks: list[MarkdownTask] = []
    for number, line in enumerate(markdown.splitlines(), start=1):
        match = _TASK.fullmatch(line)
        if match is None:
            continue
        completed = match.group("mark").casefold() == "x"
        if incomplete_only and completed:
            continue
        text = match.group("text").strip()
        folded = unicodedata.normalize("NFC", text).casefold()
        if terms and not all(term in folded for term in terms):
            continue
        due_match = _DUE.search(text)
        time_match = _TIME.search(text)
        try:
            due = date.fromisoformat(due_match.group("date")) if due_match else None
        except ValueError:
            due = None
        try:
            due_clock = time.fromisoformat(time_match.group("time")) if time_match else None
        except ValueError:
            due_clock = None
        tasks.append(
            MarkdownTask(
                text=text,
                completed=completed,
                due_date=due,
                due_time=due_clock,
                block_id=str(match.group("block") or ""),
                line_number=number,
            )
        )
    return tuple(tasks)


def _one_line(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise StructuredNoteError(f"{label} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(character in "\r\n\x00" for character in normalized)
    ):
        raise StructuredNoteError(f"{label} is invalid")
    return normalized


__all__ = ["MarkdownTask", "append_task", "list_tasks", "render_dated_task"]
