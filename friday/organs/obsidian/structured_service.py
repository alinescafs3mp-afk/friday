"""Pure orchestration for Friday-owned structured Obsidian note semantics.

This module deliberately stops before vault I/O, operation ledgers and delivery.
It composes the small deterministic codecs used by the structured acceptance
workflows and gives callers one bounded, fail-closed API over immutable inputs.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

from .base_spec import (
    BaseSpec,
    BaseSpecError,
    evaluate_base,
    friday_active_notes_spec,
    parse_base,
    render_base,
)
from .contracts import FrontmatterError, PropertyInput, PropertyType, PropertyValue
from .frontmatter import parse_frontmatter, set_frontmatter_properties
from .structured_notes import StructuredNoteError, append_section_item, render_conversation_summary
from .task_index import MarkdownTask, append_task, list_tasks, render_dated_task
from .templates import TemplateRenderError, TemplateRenderResult, render_template

MAX_NOTE_RECORDS = 2_000
_MAX_MARKDOWN_BYTES = 4 * 1024 * 1024
_MAX_RECORD_BYTES = 32 * 1024 * 1024
_MAX_PROPERTY_COUNT = 128
_MAX_PROPERTY_BYTES = 512 * 1024
_MAX_TAGS = 128
_MAX_LINKS = 128
_MAX_SUMMARY_ITEMS = 128
_MAX_TASK_RESULTS = 4_000
_MAX_PATH_CHARS = 2_048
_MAX_TEMPLATE_VALUES = 32
_KNOWN_TEMPLATE_FIELDS = frozenset({"date", "title", "project", "participants", "discussion", "actions"})
_RESERVED_PATH_ROOTS = frozenset({".obsidian", ".stfolder", ".stignore", ".stversions", ".trash"})
_INTERNAL_TRACE = re.compile(r"<\s*/?\s*(?:think|tool_call|function_call|tool)\b", re.IGNORECASE)


class StructuredServiceError(ValueError):
    """An input cannot be represented safely by the pure structured-note API."""


@dataclass(frozen=True, slots=True)
class StructuredNoteRecord:
    """One immutable note snapshot supplied to task and Base evaluation."""

    path: str
    content: str
    title: str = ""
    modified_at: date | datetime | None = None
    properties: Mapping[str, PropertyInput] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        path = _vault_path(self.path, suffix=".md")
        content = _markdown(self.content)
        try:
            parsed = parse_frontmatter(content)
        except FrontmatterError as exc:
            raise StructuredServiceError(str(exc)) from exc
        properties = (
            MappingProxyType(dict(parsed.properties))
            if self.properties is None
            else MappingProxyType(_property_snapshot(self.properties, allow_tags=True))
        )
        title = self.title or PurePosixPath(path).stem
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "title", _line(title, label="note title", maximum=500))
        object.__setattr__(self, "properties", properties)
        if self.modified_at is not None and not isinstance(self.modified_at, (date, datetime)):
            raise StructuredServiceError("modified_at must be a date, datetime, or None")

    def as_base_record(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "path": self.path,
                "title": self.title,
                "modified_at": self.modified_at,
                "properties": self.properties,
            }
        )


@dataclass(frozen=True, slots=True)
class PropertyMergeResult:
    content: str
    changed: bool
    properties: Mapping[str, PropertyValue]


@dataclass(frozen=True, slots=True)
class TaskMutationResult:
    content: str
    changed: bool
    section_reused: bool
    task: MarkdownTask
    due_at: datetime


@dataclass(frozen=True, slots=True)
class IncompleteTaskHit:
    path: str
    title: str
    task: MarkdownTask
    due_at: datetime | None


@dataclass(frozen=True, slots=True)
class SummaryLinksResult:
    content: str
    changed: bool
    added_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BaseEvaluationResult:
    """Generated `.base` bytes and the matching Friday-owned evaluation."""

    path: str
    content: str
    spec: BaseSpec
    rows: tuple[Mapping[str, Any], ...]
    evaluator: str = "friday"


class StructuredNoteService:
    """Compose deterministic structured-note primitives without performing I/O."""

    def merge_properties_and_tags(
        self,
        markdown: str,
        properties: Mapping[str, PropertyInput],
        *,
        tags: Iterable[str] = (),
    ) -> PropertyMergeResult:
        """Set typed properties and merge tags in one lossless frontmatter rewrite."""

        source = _markdown(markdown)
        updates = _property_snapshot(properties, allow_tags=False)
        requested_tags = _bounded_tags(tags)
        try:
            before = parse_frontmatter(source)
            effective = {key: value for key, value in updates.items() if before.properties.get(key) != value}
            existing_tags = before.properties.get("tags")
            if requested_tags:
                old_tags = _existing_tags(existing_tags)
                merged_tags = _deduplicate((*old_tags, *requested_tags))
                if len(merged_tags) > _MAX_TAGS:
                    raise StructuredServiceError("merged tags exceed the tag limit")
                if existing_tags != PropertyValue(PropertyType.LIST, merged_tags):
                    effective["tags"] = PropertyValue(PropertyType.LIST, merged_tags)
            rendered = set_frontmatter_properties(source, effective)
            rendered = _markdown(rendered)
            after = parse_frontmatter(rendered)
        except (FrontmatterError, StructuredNoteError) as exc:
            raise StructuredServiceError(str(exc)) from exc
        if after.body != before.body:
            raise StructuredServiceError("frontmatter mutation changed the Markdown body")
        return PropertyMergeResult(
            content=rendered,
            changed=rendered != source,
            properties=after.properties,
        )

    def add_dated_task(
        self,
        markdown: str,
        *,
        section: str,
        text: str,
        due_at: datetime,
        operation_id: str,
    ) -> TaskMutationResult:
        """Add one minute-precise local task, keyed stably by ``operation_id``."""

        source = _markdown(markdown)
        due = _local_minute(due_at)
        try:
            rendered_task = render_dated_task(
                text,
                due_date=due.date(),
                due_time=due.time(),
                operation_id=operation_id,
            )
            expected = list_tasks(rendered_task)
            if len(expected) != 1 or not expected[0].block_id:
                raise StructuredServiceError("task renderer did not produce one stable block ID")
            edit = append_task(
                source,
                section=section,
                text=text,
                due_date=due.date(),
                due_time=due.time(),
                operation_id=operation_id,
            )
            content = _markdown(edit.content)
            matches = tuple(task for task in list_tasks(content) if task.block_id == expected[0].block_id)
        except StructuredNoteError as exc:
            raise StructuredServiceError(str(exc)) from exc
        if len(matches) != 1:
            raise StructuredServiceError("stable task block ID is missing or ambiguous")
        return TaskMutationResult(
            content=content,
            changed=edit.changed,
            section_reused=edit.section_reused,
            task=matches[0],
            due_at=due,
        )

    def query_incomplete_tasks(
        self,
        notes: Iterable[StructuredNoteRecord],
        *,
        query: str = "",
    ) -> tuple[IncompleteTaskHit, ...]:
        """Query incomplete Markdown tasks across bounded, explicit note snapshots."""

        records = _records(notes)
        safe_query = _optional_line(query, label="task query", maximum=500)
        hits: list[IncompleteTaskHit] = []
        try:
            for note in records:
                for task in list_tasks(note.content, query=safe_query, incomplete_only=True):
                    due_at = (
                        datetime.combine(task.due_date, task.due_time)
                        if task.due_date is not None and task.due_time is not None
                        else None
                    )
                    hits.append(IncompleteTaskHit(note.path, note.title, task, due_at))
                    if len(hits) > _MAX_TASK_RESULTS:
                        raise StructuredServiceError("task query exceeds the result limit")
        except StructuredNoteError as exc:
            raise StructuredServiceError(str(exc)) from exc
        hits.sort(
            key=lambda hit: (
                hit.due_at is None,
                hit.due_at.isoformat() if hit.due_at is not None else "",
                hit.path.casefold(),
                hit.task.line_number,
            )
        )
        return tuple(hits)

    def render_from_template(
        self,
        template: str,
        values: Mapping[str, object],
        *,
        current_date: date,
    ) -> TemplateRenderResult:
        """Render known placeholders, fail on missing known values, preserve unknown syntax."""

        source = _markdown(template)
        if isinstance(current_date, datetime) or not isinstance(current_date, date):
            raise StructuredServiceError("current_date must be a concrete date")
        safe_values = _template_values(values)
        try:
            result = render_template(source, safe_values, current_date=current_date)
        except TemplateRenderError as exc:
            raise StructuredServiceError(str(exc)) from exc
        missing = tuple(name for name in result.unresolved if name in _KNOWN_TEMPLATE_FIELDS)
        if missing:
            raise StructuredServiceError(
                "template is missing required known values: " + ", ".join(sorted(missing))
            )
        _markdown(result.content)
        return result

    def render_summary(
        self,
        *,
        conclusions: Iterable[str],
        open_questions: Iterable[str],
        next_actions: Iterable[str],
        title: str = "Conversation Summary",
    ) -> str:
        """Render the three explicit outcome sections from bounded public facts."""

        bounded_conclusions = _summary_items(conclusions, "conclusions")
        bounded_questions = _summary_items(open_questions, "open questions")
        bounded_actions = _summary_items(next_actions, "next actions")
        try:
            rendered = render_conversation_summary(
                conclusions=bounded_conclusions,
                open_questions=bounded_questions,
                next_actions=bounded_actions,
                title=_line(title, label="summary title", maximum=500),
            )
        except StructuredNoteError as exc:
            raise StructuredServiceError(str(exc)) from exc
        return _markdown(rendered)

    def add_summary_links(
        self,
        markdown: str,
        note_paths: Iterable[str],
    ) -> SummaryLinksResult:
        """Append exact wikilinks under one managed section, idempotently."""

        source = _markdown(markdown)
        paths = _bounded_note_paths(note_paths)
        content = source
        added: list[str] = []
        try:
            for path in paths:
                target = path[: -len(".md")]
                edit = append_section_item(content, "Related notes", f"- [[{target}]]")
                content = _markdown(edit.content)
                if edit.changed:
                    added.append(path)
        except StructuredNoteError as exc:
            raise StructuredServiceError(str(exc)) from exc
        return SummaryLinksResult(content, content != source, tuple(added))

    def generate_friday_base(
        self,
        notes: Iterable[StructuredNoteRecord],
        *,
        name: str = "Friday Active Notes",
        path: str = "Bases/Friday Active Notes.base",
    ) -> BaseEvaluationResult:
        """Generate and evaluate the same supported Friday-owned BaseSpec."""

        records = _records(notes)
        base_path = _vault_path(path, suffix=".base")
        try:
            spec = friday_active_notes_spec(_line(name, label="Base name", maximum=200))
            content = render_base(spec)
            parsed = parse_base(content)
            rows = evaluate_base(parsed, tuple(note.as_base_record() for note in records))
        except BaseSpecError as exc:
            raise StructuredServiceError(str(exc)) from exc
        frozen_rows = tuple(MappingProxyType(dict(row)) for row in rows)
        return BaseEvaluationResult(base_path, content, parsed, frozen_rows)


def _markdown(value: object) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise StructuredServiceError("Markdown must be text without NUL")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise StructuredServiceError("Markdown must be valid UTF-8") from exc
    if len(encoded) > _MAX_MARKDOWN_BYTES:
        raise StructuredServiceError("Markdown exceeds the structured-note size limit")
    return value


def _line(value: object, *, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise StructuredServiceError(f"{label} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > maximum or any(char in "\r\n\x00" for char in normalized):
        raise StructuredServiceError(f"{label} is invalid")
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in normalized):
        raise StructuredServiceError(f"{label} contains a control character")
    return normalized


def _optional_line(value: object, *, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise StructuredServiceError(f"{label} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if len(normalized) > maximum or any(char in "\r\n\x00" for char in normalized):
        raise StructuredServiceError(f"{label} is invalid")
    return normalized


def _vault_path(value: object, *, suffix: str) -> str:
    path = _line(value, label="vault path", maximum=_MAX_PATH_CHARS)
    if (
        "\\" in path
        or path.startswith("/")
        or (len(path) >= 2 and path[0].isalpha() and path[1] == ":")
        or any(char in "[]|#" for char in path)
    ):
        raise StructuredServiceError("vault path is not a safe relative POSIX path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise StructuredServiceError("vault path contains an unsafe segment")
    folded = tuple(part.casefold() for part in parts)
    if folded[0] in _RESERVED_PATH_ROOTS or ".obsidian" in folded:
        raise StructuredServiceError("vault path enters a reserved directory")
    if ".sync-conflict-" in folded[-1]:
        raise StructuredServiceError("vault path names a conflict copy")
    if PurePosixPath(path).suffix.casefold() != suffix:
        raise StructuredServiceError(f"vault path must end with {suffix}")
    return path


def _property_snapshot(
    properties: Mapping[str, PropertyInput],
    *,
    allow_tags: bool,
) -> dict[str, PropertyValue]:
    if not isinstance(properties, Mapping):
        raise StructuredServiceError("properties must be a mapping")
    if len(properties) > _MAX_PROPERTY_COUNT:
        raise StructuredServiceError("property update exceeds the field limit")
    total = 0
    snapshot: dict[str, PropertyValue] = {}
    for raw_key, raw_value in properties.items():
        key = _line(raw_key, label="property name", maximum=128)
        if any(char in key for char in ":") or key.startswith(("#", "-")):
            raise StructuredServiceError("property name contains YAML syntax")
        if not allow_tags and key.casefold() == "tags":
            raise StructuredServiceError("tags must be supplied through the merge-tags argument")
        try:
            value = PropertyValue.coerce(raw_value)
        except FrontmatterError as exc:
            raise StructuredServiceError(str(exc)) from exc
        total += len(key.encode("utf-8")) + _property_size(value)
        if total > _MAX_PROPERTY_BYTES:
            raise StructuredServiceError("properties exceed the structured-note byte limit")
        snapshot[key] = value
    return snapshot


def _property_size(value: PropertyValue) -> int:
    raw = value.as_python()
    if isinstance(raw, tuple):
        return sum(len(item.encode("utf-8")) for item in raw)
    return len(str(raw).encode("utf-8"))


def _tag(value: object) -> str:
    tag = _line(value, label="tag", maximum=128).removeprefix("#")
    if not tag or any(char.isspace() or char in ",[]" for char in tag):
        raise StructuredServiceError("tag contains unsupported syntax")
    return tag


def _bounded_tags(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise StructuredServiceError("tags must be an iterable of strings")
    bounded: list[str] = []
    for value in values:
        if len(bounded) >= _MAX_TAGS:
            raise StructuredServiceError("tag update exceeds the tag limit")
        bounded.append(_tag(value))
    return _deduplicate(tuple(bounded))


def _existing_tags(value: PropertyValue | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if value.type is PropertyType.LIST:
        raw = value.value
        if not isinstance(raw, tuple):
            raise StructuredServiceError("existing tags have an invalid list representation")
        tags = tuple(_tag(item) for item in raw)
    elif value.type is PropertyType.TEXT and isinstance(value.value, str):
        tags = () if not value.value.strip() else (_tag(value.value),)
    else:
        raise StructuredServiceError("existing tags are not a text or list property")
    if len(tags) > _MAX_TAGS:
        raise StructuredServiceError("existing tags exceed the tag limit")
    return _deduplicate(tags)


def _deduplicate(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        folded = unicodedata.normalize("NFC", value).casefold()
        if folded in seen:
            continue
        seen.add(folded)
        result.append(value)
    return tuple(result)


def _local_minute(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise StructuredServiceError("due_at must be a datetime")
    if value.tzinfo is not None or value.second or value.microsecond:
        raise StructuredServiceError("due_at must be a minute-precise local wall datetime")
    return value


def _records(values: Iterable[StructuredNoteRecord]) -> tuple[StructuredNoteRecord, ...]:
    if isinstance(values, (str, bytes, Mapping)) or not isinstance(values, Iterable):
        raise StructuredServiceError("notes must be an iterable of StructuredNoteRecord values")
    records: list[StructuredNoteRecord] = []
    seen: set[str] = set()
    total = 0
    for value in values:
        if len(records) >= MAX_NOTE_RECORDS:
            raise StructuredServiceError("note input exceeds the record limit")
        if not isinstance(value, StructuredNoteRecord):
            raise StructuredServiceError("note input contains a non-record value")
        folded = value.path.casefold()
        if folded in seen:
            raise StructuredServiceError("note input contains an ambiguous duplicate path")
        seen.add(folded)
        total += len(value.content.encode("utf-8"))
        if total > _MAX_RECORD_BYTES:
            raise StructuredServiceError("note input exceeds the aggregate byte limit")
        records.append(value)
    return tuple(records)


def _template_values(values: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(values, Mapping):
        raise StructuredServiceError("template values must be a mapping")
    if len(values) > _MAX_TEMPLATE_VALUES:
        raise StructuredServiceError("template values exceed the field limit")
    snapshot: dict[str, object] = {}
    total = 0
    for raw_key, raw_value in values.items():
        key = _line(raw_key, label="template field", maximum=64)
        if isinstance(raw_value, (list, tuple)):
            if not raw_value or len(raw_value) > _MAX_SUMMARY_ITEMS:
                raise StructuredServiceError("template list value is empty or too large")
            tuple_value = tuple(
                _line(item, label=f"template value {key}", maximum=100_000) for item in raw_value
            )
            value: object = tuple_value
            total += sum(len(item.encode("utf-8")) for item in tuple_value)
        else:
            normalized = _line(raw_value, label=f"template value {key}", maximum=100_000)
            value = normalized
            total += len(normalized.encode("utf-8"))
        if total > _MAX_PROPERTY_BYTES:
            raise StructuredServiceError("template values exceed the aggregate byte limit")
        snapshot[key] = value
    return MappingProxyType(snapshot)


def _summary_items(values: Iterable[str], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise StructuredServiceError(f"{label} must be an iterable of strings")
    items: list[str] = []
    for value in values:
        if len(items) >= _MAX_SUMMARY_ITEMS:
            raise StructuredServiceError(f"{label} exceeds the item limit")
        item = _line(value, label=label, maximum=10_000)
        if _INTERNAL_TRACE.search(item) is not None:
            raise StructuredServiceError(f"{label} contains internal protocol markup")
        items.append(item)
    return tuple(items)


def _bounded_note_paths(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise StructuredServiceError("note paths must be an iterable of strings")
    paths: list[str] = []
    seen: set[str] = set()
    for value in values:
        if len(paths) >= _MAX_LINKS:
            raise StructuredServiceError("summary links exceed the link limit")
        path = _vault_path(value, suffix=".md")
        folded = path.casefold()
        if folded not in seen:
            seen.add(folded)
            paths.append(path)
    return tuple(paths)


__all__ = [
    "BaseEvaluationResult",
    "IncompleteTaskHit",
    "MAX_NOTE_RECORDS",
    "PropertyMergeResult",
    "StructuredNoteRecord",
    "StructuredNoteService",
    "StructuredServiceError",
    "SummaryLinksResult",
    "TaskMutationResult",
]
