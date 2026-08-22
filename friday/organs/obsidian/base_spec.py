"""Friday-owned evaluator for the supported Obsidian Bases subset."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import yaml  # type: ignore[import-untyped]

from .contracts import PropertyValue

_EXPRESSION = re.compile(
    r"^(?P<property>[A-Za-z_][A-Za-z0-9_.-]{0,127})\s*"
    r'(?P<operator>==|!=)\s*"(?P<value>(?:[^"\\]|\\.){0,1000})"$'
)
_COLUMNS = frozenset({"file.name", "file.path", "file.mtime", "title", "status"})


class BaseSpecError(ValueError):
    """A Base file is outside the supported deterministic subset."""


@dataclass(frozen=True, slots=True)
class BaseFilter:
    property: str
    operator: str
    value: str


@dataclass(frozen=True, slots=True)
class BaseSpec:
    name: str
    filters: tuple[BaseFilter, ...]
    columns: tuple[str, ...]
    sort_by: str = "file.mtime"
    sort_descending: bool = True


def friday_active_notes_spec(name: str = "Friday Active Notes") -> BaseSpec:
    return BaseSpec(
        name=_line(name, "Base name", 200),
        filters=(
            BaseFilter("project", "==", "Friday"),
            BaseFilter("status", "!=", "done"),
        ),
        columns=("file.name", "status", "file.mtime"),
    )


def render_base(spec: BaseSpec) -> str:
    checked = _validate_spec(spec)
    payload = {
        "filters": {
            "and": [
                f"{item.property} {item.operator} {json.dumps(item.value, ensure_ascii=False)}"
                for item in checked.filters
            ]
        },
        "views": [
            {
                "type": "table",
                "name": checked.name,
                "order": list(checked.columns),
                "sort": [
                    {
                        "property": checked.sort_by,
                        "direction": "DESC" if checked.sort_descending else "ASC",
                    }
                ],
            }
        ],
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=1000)


def parse_base(content: str) -> BaseSpec:
    if not isinstance(content, str) or not content or len(content) > 512_000 or "\x00" in content:
        raise BaseSpecError("Base content is empty or too large")
    try:
        payload = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise BaseSpecError("Base content is not valid YAML") from exc
    if not isinstance(payload, Mapping) or set(payload) != {"filters", "views"}:
        raise BaseSpecError("Base content has unsupported fields")
    filters_raw = payload.get("filters")
    if not isinstance(filters_raw, Mapping) or set(filters_raw) != {"and"}:
        raise BaseSpecError("Base filters must contain one and-list")
    expressions = filters_raw.get("and")
    if not isinstance(expressions, list) or not 1 <= len(expressions) <= 32:
        raise BaseSpecError("Base filters are missing or too numerous")
    filters: list[BaseFilter] = []
    for expression in expressions:
        if not isinstance(expression, str):
            raise BaseSpecError("Base filter must be an expression")
        match = _EXPRESSION.fullmatch(expression)
        if match is None:
            raise BaseSpecError("Base filter expression is unsupported")
        try:
            value = json.loads(f'"{match.group("value")}"')
        except json.JSONDecodeError as exc:
            raise BaseSpecError("Base filter string is invalid") from exc
        if not isinstance(value, str):
            raise BaseSpecError("Base filter value must be text")
        filters.append(BaseFilter(match.group("property"), match.group("operator"), value))
    views = payload.get("views")
    if not isinstance(views, list) or len(views) != 1 or not isinstance(views[0], Mapping):
        raise BaseSpecError("exactly one table view is supported")
    view = views[0]
    if set(view) != {"type", "name", "order", "sort"} or view.get("type") != "table":
        raise BaseSpecError("Base view is outside the supported table subset")
    columns_raw = view.get("order")
    if not isinstance(columns_raw, list) or not columns_raw or len(columns_raw) > 32:
        raise BaseSpecError("Base columns are invalid")
    columns = tuple(str(item) for item in columns_raw)
    sort_raw = view.get("sort")
    if not isinstance(sort_raw, list) or len(sort_raw) != 1 or not isinstance(sort_raw[0], Mapping):
        raise BaseSpecError("Base sort is invalid")
    sort = sort_raw[0]
    if set(sort) != {"property", "direction"} or sort.get("direction") not in {"ASC", "DESC"}:
        raise BaseSpecError("Base sort is outside the supported subset")
    return _validate_spec(
        BaseSpec(
            name=_line(view.get("name"), "Base name", 200),
            filters=tuple(filters),
            columns=columns,
            sort_by=str(sort.get("property")),
            sort_descending=sort.get("direction") == "DESC",
        )
    )


def evaluate_base(spec: BaseSpec, notes: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    checked = _validate_spec(spec)
    if len(notes) > 20_000:
        raise BaseSpecError("Base input exceeds the note limit")
    rows: list[dict[str, Any]] = []
    for note in notes:
        if not isinstance(note, Mapping):
            raise BaseSpecError("Base note must be a mapping")
        properties_raw = note.get("properties")
        properties = properties_raw if isinstance(properties_raw, Mapping) else {}
        if not all(_matches(properties, condition) for condition in checked.filters):
            continue
        row = {column: _column(note, properties, column) for column in checked.columns}
        rows.append(row)
    rows.sort(
        key=lambda row: _sortable(row.get(checked.sort_by)),
        reverse=checked.sort_descending,
    )
    return tuple(rows)


def _matches(properties: Mapping[str, Any], condition: BaseFilter) -> bool:
    actual = _property(properties.get(condition.property))
    expected = unicodedata.normalize("NFC", condition.value).casefold()
    equal = isinstance(actual, str) and unicodedata.normalize("NFC", actual).casefold() == expected
    return equal if condition.operator == "==" else not equal


def _property(value: Any) -> Any:
    return value.as_python() if isinstance(value, PropertyValue) else value


def _column(note: Mapping[str, Any], properties: Mapping[str, Any], column: str) -> Any:
    path = str(note.get("path") or "")
    if column == "file.name":
        return path.rsplit("/", 1)[-1].removesuffix(".md")
    if column == "file.path":
        return path
    if column == "file.mtime":
        value = note.get("modified_at")
        return value.isoformat() if isinstance(value, (date, datetime)) else str(value or "")
    if column == "title":
        return str(note.get("title") or "")
    return _property(properties.get(column))


def _validate_spec(spec: BaseSpec) -> BaseSpec:
    if not isinstance(spec, BaseSpec) or not spec.filters or len(spec.filters) > 32:
        raise BaseSpecError("BaseSpec is invalid")
    _line(spec.name, "Base name", 200)
    for item in spec.filters:
        if not isinstance(item, BaseFilter) or item.operator not in {"==", "!="}:
            raise BaseSpecError("Base filter is invalid")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", item.property) is None:
            raise BaseSpecError("Base property is invalid")
        _line(item.value, "Base filter value", 1000)
    if not spec.columns or len(spec.columns) > 32 or any(column not in _COLUMNS for column in spec.columns):
        raise BaseSpecError("Base column is unsupported")
    if spec.sort_by not in spec.columns:
        raise BaseSpecError("Base sort column must be visible")
    return spec


def _sortable(value: Any) -> tuple[int, str]:
    return (1, "") if value is None else (0, str(value).casefold())


def _line(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise BaseSpecError(f"{label} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > maximum or any(char in "\r\n\x00" for char in normalized):
        raise BaseSpecError(f"{label} is invalid")
    return normalized


__all__ = [
    "BaseFilter",
    "BaseSpec",
    "BaseSpecError",
    "evaluate_base",
    "friday_active_notes_spec",
    "parse_base",
    "render_base",
]
