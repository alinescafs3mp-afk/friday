"""A deliberately small Obsidian frontmatter codec with lossless body updates.

The note core does not accept arbitrary YAML from a model.  It reads the common
Obsidian scalar/list subset and rewrites only explicitly selected top-level
properties, leaving unknown frontmatter blocks and the Markdown body alone.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType

from .contracts import FrontmatterError, InvalidPropertyError, PropertyInput, PropertyType, PropertyValue

_KEY = re.compile(r"^([^:#][^:]{0,127}):(?:[ \t]*(.*))?$")
_NUMBER = re.compile(r"[-+]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?")
_LIST_ITEM = re.compile(r"^[ \t]+-[ \t]*(.*)$")


@dataclass(frozen=True, slots=True)
class _Block:
    key: str | None
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ParsedFrontmatter:
    properties: MappingProxyType[str, PropertyValue]
    body: str
    has_frontmatter: bool
    newline: str
    _blocks: tuple[_Block, ...]


def parse_frontmatter(markdown: str) -> ParsedFrontmatter:
    """Parse supported typed values while retaining raw blocks for safe edits."""

    _validate_markdown(markdown)
    header, body, has_frontmatter, newline = _partition(markdown)
    if not has_frontmatter:
        return ParsedFrontmatter(MappingProxyType({}), markdown, False, newline, ())

    blocks = _parse_blocks(header)
    properties: dict[str, PropertyValue] = {}
    for block in blocks:
        if block.key is None:
            continue
        if block.key in properties:
            raise FrontmatterError(f"duplicate frontmatter property: {block.key}")
        properties[block.key] = _decode_block(block)
    return ParsedFrontmatter(MappingProxyType(properties), body, True, newline, tuple(blocks))


def set_frontmatter_properties(markdown: str, updates: Mapping[str, PropertyInput]) -> str:
    """Atomically compose a typed multi-property rewrite in memory.

    Existing blocks not named in ``updates`` are emitted byte-for-byte.  The
    Markdown body is always returned exactly as it was received.
    """

    if not isinstance(updates, Mapping):
        raise InvalidPropertyError("property updates must be a mapping")
    normalized: dict[str, PropertyValue] = {}
    for key, value in updates.items():
        normalized[_validate_key(key)] = PropertyValue.coerce(value)
    if not normalized:
        _validate_markdown(markdown)
        return markdown

    parsed = parse_frontmatter(markdown)
    newline = parsed.newline
    remaining = dict(normalized)
    rendered: list[str] = []
    for block in parsed._blocks:
        if block.key is not None and block.key in remaining:
            rendered.extend(_render_property(block.key, remaining.pop(block.key), newline=newline))
        else:
            rendered.extend(block.lines)
    for key, value in remaining.items():
        if rendered and not rendered[-1].endswith(("\n", "\r")):
            rendered[-1] += newline
        rendered.extend(_render_property(key, value, newline=newline))

    header = "".join(rendered)
    if header and not header.endswith(("\n", "\r")):
        header += newline
    return f"---{newline}{header}---{newline}{parsed.body}"


def _validate_markdown(markdown: str) -> None:
    if not isinstance(markdown, str):
        raise FrontmatterError("Markdown content must be a string")
    if "\x00" in markdown:
        raise FrontmatterError("Markdown content must not contain NUL")
    try:
        markdown.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise FrontmatterError("Markdown content must be valid UTF-8") from exc


def _partition(markdown: str) -> tuple[tuple[str, ...], str, bool, str]:
    lines = markdown.splitlines(keepends=True)
    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    if not lines or lines[0].rstrip("\r\n") != "---":
        return (), markdown, False, newline
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == "---":
            return tuple(lines[1:index]), "".join(lines[index + 1 :]), True, newline
    raise FrontmatterError("frontmatter opening delimiter has no closing delimiter")


def _parse_blocks(lines: tuple[str, ...]) -> list[_Block]:
    blocks: list[_Block] = []
    current_key: str | None = None
    current_lines: list[str] = []

    def finish() -> None:
        nonlocal current_key, current_lines
        if current_lines:
            blocks.append(_Block(current_key, tuple(current_lines)))
        current_key = None
        current_lines = []

    for line in lines:
        logical = line.rstrip("\r\n")
        match = _KEY.fullmatch(logical) if logical and not logical[0].isspace() else None
        if match is not None:
            finish()
            current_key = _validate_key(match.group(1).strip())
            current_lines = [line]
            continue
        if not logical or logical.lstrip().startswith("#"):
            finish()
            blocks.append(_Block(None, (line,)))
            continue
        if logical[0].isspace() and current_key is not None:
            current_lines.append(line)
            continue
        finish()
        blocks.append(_Block(None, (line,)))
    finish()
    return blocks


def _decode_block(block: _Block) -> PropertyValue:
    first = block.lines[0].rstrip("\r\n")
    match = _KEY.fullmatch(first)
    if match is None:  # pragma: no cover - guaranteed by block construction
        raise FrontmatterError("invalid property block")
    scalar = (match.group(2) or "").strip()
    continuations = block.lines[1:]
    if continuations:
        values: list[str] = []
        for line in continuations:
            list_match = _LIST_ITEM.fullmatch(line.rstrip("\r\n"))
            if list_match is None:
                return PropertyValue(PropertyType.TEXT, "".join(block.lines).rstrip("\r\n"))
            item = _decode_scalar(list_match.group(1))
            if item.type is not PropertyType.TEXT:
                values.append(str(item.value))
            else:
                values.append(str(item.value))
        if not scalar:
            return PropertyValue(PropertyType.LIST, tuple(values))
        return PropertyValue(PropertyType.TEXT, "".join(block.lines).rstrip("\r\n"))
    return _decode_scalar(scalar)


def _decode_scalar(raw: str) -> PropertyValue:
    if not raw:
        return PropertyValue(PropertyType.TEXT, "")
    if raw.startswith('"') and raw.endswith('"'):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return PropertyValue(PropertyType.TEXT, raw)
        if isinstance(decoded, str):
            return PropertyValue(PropertyType.TEXT, decoded)
    if raw.startswith("'") and raw.endswith("'"):
        return PropertyValue(PropertyType.TEXT, raw[1:-1].replace("''", "'"))
    if raw.startswith("[") and raw.endswith("]"):
        decoded_list = _decode_inline_list(raw)
        if decoded_list is not None:
            return PropertyValue(PropertyType.LIST, decoded_list)
    lowered = raw.casefold()
    if lowered in {"true", "false"}:
        return PropertyValue(PropertyType.CHECKBOX, lowered == "true")
    if _NUMBER.fullmatch(raw):
        try:
            number = float(raw) if any(char in raw for char in ".eE") else int(raw)
        except ValueError:  # pragma: no cover - regex has already bounded the shape
            pass
        else:
            return PropertyValue(PropertyType.NUMBER, number)
    if "T" in raw or " " in raw:
        try:
            parsed_datetime = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            return PropertyValue(PropertyType.DATETIME, parsed_datetime)
    try:
        parsed_date = date.fromisoformat(raw)
    except ValueError:
        return PropertyValue(PropertyType.TEXT, raw)
    return PropertyValue(PropertyType.DATE, parsed_date)


def _decode_inline_list(raw: str) -> tuple[str, ...] | None:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, list) and all(isinstance(item, str) for item in decoded):
        return tuple(decoded)
    inner = raw[1:-1].strip()
    if not inner:
        return ()
    values: list[str] = []
    for item in inner.split(","):
        value = _decode_scalar(item.strip())
        if value.type is not PropertyType.TEXT:
            return None
        values.append(str(value.value))
    return tuple(values)


def _render_property(key: str, value: PropertyValue, *, newline: str) -> list[str]:
    if value.type is PropertyType.LIST:
        list_value = value.value
        if not isinstance(list_value, tuple):  # pragma: no cover - PropertyValue invariant
            raise InvalidPropertyError("list property must contain strings")
        if not list_value:
            return [f"{key}: []{newline}"]
        lines = [f"{key}:{newline}"]
        lines.extend(f"  - {json.dumps(item, ensure_ascii=False)}{newline}" for item in list_value)
        return lines
    if value.type is PropertyType.TEXT:
        scalar = json.dumps(value.value, ensure_ascii=False)
    elif value.type is PropertyType.NUMBER:
        scalar = repr(value.value)
    elif value.type is PropertyType.CHECKBOX:
        scalar = "true" if value.value else "false"
    elif value.type in {PropertyType.DATE, PropertyType.DATETIME}:
        temporal_value = value.value
        if not isinstance(temporal_value, (date, datetime)):  # pragma: no cover - invariant
            raise InvalidPropertyError("temporal property has an invalid value")
        scalar = temporal_value.isoformat()
    else:  # pragma: no cover - PropertyValue rejects unknown enum members
        raise InvalidPropertyError("unsupported property type")
    return [f"{key}: {scalar}{newline}"]


def _validate_key(key: object) -> str:
    if not isinstance(key, str):
        raise InvalidPropertyError("property name must be a string")
    if not key or key != key.strip() or len(key) > 128:
        raise InvalidPropertyError("property name must be non-empty, trimmed, and at most 128 characters")
    if any(character in key for character in (":", "\r", "\n", "\x00")) or key.startswith(("#", "-")):
        raise InvalidPropertyError("property name contains YAML syntax")
    try:
        key.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise InvalidPropertyError("property name must be valid UTF-8") from exc
    return key


__all__ = ["ParsedFrontmatter", "parse_frontmatter", "set_frontmatter_properties"]
