"""Deterministic Markdown structures used by the Obsidian acceptance workflows."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

_MAX_MARKDOWN_CHARS = 4 * 1024 * 1024
_MAX_FIELD_CHARS = 200_000
_HEADING = re.compile(r"^(?P<marks>#{1,6})[ \t]+(?P<title>.*?)[ \t]*#*[ \t]*$")


class StructuredNoteError(ValueError):
    """A requested structured edit is unsafe or ambiguous."""


@dataclass(frozen=True, slots=True)
class StructuredEdit:
    content: str
    changed: bool
    section_reused: bool


def append_section_item(
    markdown: str,
    heading: str,
    item: str,
    *,
    heading_level: int = 2,
) -> StructuredEdit:
    """Append one item to one exact section, creating that section when absent.

    Matching is Unicode-normalized and case-insensitive.  Two equal headings are
    ambiguous and fail closed.  Existing text is otherwise retained byte for byte.
    """

    source = _markdown(markdown)
    title = _field(heading, label="section heading", maximum=500, multiline=False)
    addition = _field(item, label="section item", maximum=_MAX_FIELD_CHARS, multiline=True)
    if isinstance(heading_level, bool) or not isinstance(heading_level, int) or not 1 <= heading_level <= 6:
        raise StructuredNoteError("heading_level must be between 1 and 6")
    newline = _newline(source)
    lines = source.splitlines(keepends=True)
    wanted = _fold(title)
    matches: list[tuple[int, int]] = []
    headings = _markdown_headings(lines)
    for position, (index, level, parsed_title) in enumerate(headings):
        if level == heading_level and _fold(parsed_title) == wanted:
            matches.append((index, _section_end(headings, position, heading_level, len(lines))))
    if len(matches) > 1:
        raise StructuredNoteError("section heading is ambiguous")
    if not matches:
        separator = "" if not source else newline if source.endswith(("\n", "\r")) else newline * 2
        rendered = f"{source}{separator}{'#' * heading_level} {title}{newline}{newline}{addition}"
        if not rendered.endswith(("\n", "\r")):
            rendered += newline
        return StructuredEdit(rendered, True, False)

    start, end = matches[0]
    logical_addition = addition.rstrip("\r\n")
    section_text = "".join(lines[start + 1 : end])
    if any(line.rstrip("\r\n") == logical_addition for line in section_text.splitlines(keepends=True)):
        return StructuredEdit(source, False, True)
    insertion = logical_addition + newline
    if end > start + 1 and lines[end - 1].strip():
        insertion = newline + insertion
    lines.insert(end, insertion)
    return StructuredEdit("".join(lines), True, True)


def replace_section(markdown: str, heading: str, replacement: str, *, heading_level: int = 2) -> str:
    """Replace the body of exactly one section while retaining its heading."""

    source = _markdown(markdown)
    title = _field(heading, label="section heading", maximum=500, multiline=False)
    body = _field(replacement, label="replacement", maximum=_MAX_FIELD_CHARS, multiline=True)
    lines = source.splitlines(keepends=True)
    wanted = _fold(title)
    matches: list[tuple[int, int]] = []
    headings = _markdown_headings(lines)
    for position, (index, level, parsed_title) in enumerate(headings):
        if level == heading_level and _fold(parsed_title) == wanted:
            matches.append((index, _section_end(headings, position, heading_level, len(lines))))
    if len(matches) != 1:
        raise StructuredNoteError("section must resolve to exactly one heading")
    start, end = matches[0]
    newline = _newline(source)
    replacement_lines = [newline, body.rstrip("\r\n") + newline]
    return "".join([*lines[: start + 1], *replacement_lines, *lines[end:]])


def render_conversation_summary(
    *,
    conclusions: Iterable[str],
    open_questions: Iterable[str],
    next_actions: Iterable[str],
    title: str = "Conversation Summary",
) -> str:
    """Render the three explicit, user-visible summary sections."""

    safe_title = _field(title, label="summary title", maximum=500, multiline=False)
    sections = (
        ("Conclusions", conclusions),
        ("Open questions", open_questions),
        ("Next actions", next_actions),
    )
    chunks = [f"# {safe_title}"]
    for heading, values in sections:
        items = [_field(value, label=heading, maximum=10_000, multiline=False) for value in values]
        chunks.append(f"## {heading}\n\n" + ("\n".join(f"- {item}" for item in items) or "- Нет"))
    return "\n\n".join(chunks) + "\n"


def _markdown(value: object) -> str:
    if not isinstance(value, str) or len(value) > _MAX_MARKDOWN_CHARS or "\x00" in value:
        raise StructuredNoteError("Markdown is invalid or exceeds the size limit")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise StructuredNoteError("Markdown must be valid UTF-8") from exc
    return value


def _field(value: object, *, label: str, maximum: int, multiline: bool) -> str:
    if not isinstance(value, str):
        raise StructuredNoteError(f"{label} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise StructuredNoteError(f"{label} is empty or too large")
    if not multiline and any(character in "\r\n" for character in normalized):
        raise StructuredNoteError(f"{label} must be one line")
    if any(
        unicodedata.category(character) in {"Cc", "Cs"} and character not in "\r\n\t"
        for character in normalized
    ):
        raise StructuredNoteError(f"{label} contains a control character")
    return normalized


def _newline(markdown: str) -> str:
    return "\r\n" if "\r\n" in markdown else "\n"


def _heading(line: str) -> tuple[int, str] | None:
    match = _HEADING.fullmatch(line.rstrip("\r\n"))
    return None if match is None else (len(match.group("marks")), match.group("title").strip())


def markdown_headings(markdown: str) -> tuple[tuple[int, str], ...]:
    """Return only real ATX headings outside frontmatter, fences and comments."""

    source = _markdown(markdown)
    return tuple((level, title) for _index, level, title in _markdown_headings(source.splitlines(True)))


def _markdown_headings(lines: list[str]) -> list[tuple[int, int, str]]:
    headings: list[tuple[int, int, str]] = []
    in_frontmatter = bool(lines and lines[0].lstrip("\ufeff").rstrip("\r\n") == "---")
    fence: tuple[str, int] | None = None
    in_comment = False
    for index, line in enumerate(lines):
        stripped = line.rstrip("\r\n")
        if in_frontmatter:
            if index > 0 and stripped in {"---", "..."}:
                in_frontmatter = False
            continue
        marker = _fence_marker(stripped)
        if fence is not None:
            if marker is not None and marker[0] == fence[0] and marker[1] >= fence[1]:
                fence = None
            continue
        if marker is not None:
            fence = marker
            continue
        if in_comment:
            if "-->" in stripped:
                in_comment = False
            continue
        if "<!--" in stripped:
            if "-->" not in stripped.split("<!--", 1)[1]:
                in_comment = True
            continue
        parsed = _heading(line)
        if parsed is not None:
            headings.append((index, parsed[0], parsed[1]))
    return headings


def _section_end(
    headings: list[tuple[int, int, str]],
    position: int,
    level: int,
    line_count: int,
) -> int:
    for index, candidate_level, _title in headings[position + 1 :]:
        if candidate_level <= level:
            return index
    return line_count


def _fence_marker(line: str) -> tuple[str, int] | None:
    cursor = 0
    while cursor < len(line) and cursor < 4 and line[cursor] == " ":
        cursor += 1
    if cursor > 3 or cursor >= len(line) or line[cursor] not in {"`", "~"}:
        return None
    marker = line[cursor]
    end = cursor
    while end < len(line) and line[end] == marker:
        end += 1
    width = end - cursor
    return (marker, width) if width >= 3 else None


def _fold(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold().strip()


__all__ = [
    "StructuredEdit",
    "StructuredNoteError",
    "append_section_item",
    "markdown_headings",
    "render_conversation_summary",
    "replace_section",
]
