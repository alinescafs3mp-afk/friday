"""Bounded, literal placeholder rendering for user-owned Obsidian templates."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

_PLACEHOLDER = re.compile(r"\{\{(?P<name>[A-Za-z][A-Za-z0-9_-]{0,63})\}\}")
_SUPPORTED = frozenset({"date", "title", "project", "participants", "discussion", "actions"})


class TemplateRenderError(ValueError):
    """Template input is invalid or a required supplied value is missing."""


@dataclass(frozen=True, slots=True)
class TemplateRenderResult:
    content: str
    resolved: tuple[str, ...]
    unresolved: tuple[str, ...]


def render_template(
    template: str,
    values: Mapping[str, object],
    *,
    current_date: date,
) -> TemplateRenderResult:
    """Replace the supported common placeholders and preserve unknown syntax."""

    if not isinstance(template, str) or not template or len(template) > 4 * 1024 * 1024:
        raise TemplateRenderError("template is empty or too large")
    if "\x00" in template:
        raise TemplateRenderError("template contains NUL")
    if not isinstance(values, Mapping) or not isinstance(current_date, date):
        raise TemplateRenderError("template values and date are invalid")
    normalized: dict[str, str] = {"date": current_date.isoformat()}
    for key, value in values.items():
        if not isinstance(key, str) or key not in _SUPPORTED - {"date"}:
            raise TemplateRenderError("unsupported template value")
        if isinstance(value, (list, tuple)):
            if not value or any(not isinstance(item, str) for item in value):
                raise TemplateRenderError(f"template value {key!r} must contain text")
            rendered = ", ".join(_value(item, key) for item in value)
        else:
            rendered = _value(value, key)
        normalized[key] = rendered

    resolved: list[str] = []
    unresolved: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name in normalized:
            if name not in resolved:
                resolved.append(name)
            return normalized[name]
        if name not in unresolved:
            unresolved.append(name)
        return match.group(0)

    rendered = _PLACEHOLDER.sub(replace, template)
    return TemplateRenderResult(rendered, tuple(resolved), tuple(unresolved))


def _value(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TemplateRenderError(f"template value {name!r} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > 100_000 or "\x00" in normalized:
        raise TemplateRenderError(f"template value {name!r} is empty or too large")
    return normalized


__all__ = ["TemplateRenderError", "TemplateRenderResult", "render_template"]
