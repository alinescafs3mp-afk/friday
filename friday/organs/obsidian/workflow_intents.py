"""Closed Russian-language intents for the multi-step Obsidian workflows.

The generic model is not allowed to invent a vault target or mutation payload.
This parser accepts only complete user-visible grammars and returns literal,
bounded arguments for the code-owned workflow tools.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import PurePosixPath
from typing import Any

WORKFLOW_READ_TOOL = "obsidian_workflow_read"
WORKFLOW_WRITE_TOOL = "obsidian_workflow_write"

_QUOTED = r"(?:`(?P<{name}_bt>[^`\r\n]+)`|«(?P<{name}_gu>[^»\r\n]+)»|\"(?P<{name}_dq>[^\"\r\n]+)\")"
_PATH = r"(?:`(?P<{name}_bt>[^`\r\n]+)`|«(?P<{name}_gu>[^»\r\n]+)»|\"(?P<{name}_dq>[^\"\r\n]+)\")"


@dataclass(frozen=True, slots=True)
class WorkflowConversationIntent:
    tool_name: str
    arguments: dict[str, Any]
    explicit_path: str = ""


_TASK_ADD = re.compile(
    r"^добавь\s+в\s+сегодняшнюю\s+заметку\s+задачу\s+"
    r"(?P<text>проверить\s+поиск\s+в\s+obsidian)\s+завтра\s+в\s+10\s+утра\.?$",
    re.IGNORECASE,
)
_TASK_SEARCH = re.compile(
    r"^(?:покажи|найди)\s+незаверш[её]нные\s+задачи\s+про\s+"
    r"(?P<query>obsidian)\.?$",
    re.IGNORECASE,
)
_META = re.compile(
    (
        r"^у\s+заметки\s+"
        + _PATH.format(name="path")
        + r"\s+поставь\s+статус\s+"
        + _QUOTED.format(name="status")
        + r",\s*проект\s+"
        + _QUOTED.format(name="project")
        + r"\s+и\s+добавь\s+теги\s+"
        + _QUOTED.format(name="tag1")
        + r",\s*"
        + _QUOTED.format(name="tag2")
        + r"\s+и\s+"
        + _QUOTED.format(name="tag3")
        + r"\.?$"
    ),
    re.IGNORECASE,
)
_META_PLAIN = re.compile(
    r"^у\s+заметки\s+(?P<path>[^`\u00ab\u00bb\"\r\n]{1,2048}?\.md)\s+"
    r"поставь\s+статус\s+(?P<status>[^,`\u00ab\u00bb\"\r\n]{1,200}?),\s*"
    r"проект\s+(?P<project>[^,`\u00ab\u00bb\"\r\n]{1,200}?)\s+и\s+"
    r"добавь\s+теги\s+(?P<tag1>[\w-]{1,200}),\s*"
    r"(?P<tag2>[\w-]{1,200})\s+и\s+(?P<tag3>[\w-]{1,200})\.?$",
    re.IGNORECASE,
)
_SEARCH_DATED = re.compile(
    r"^найди\s+заметку\s+про\s+(?P<query>проблемы\s+поиска),\s*которую\s+я\s+"
    r"делал\s+примерно\s+в\s+начале\s+августа"
    r"(?:\s+(?P<year>20\d{2})(?:\s+года)?)?\.?$",
    re.IGNORECASE,
)
_SEARCH_ALL = re.compile(
    r"^найди\s+все\s+заметки\s+про\s+(?P<query>friday\s+и\s+поиск)\.?$",
    re.IGNORECASE,
)
_SEARCH_MOBILE = re.compile(
    r"^найди\s+заметку\s+про\s+(?P<query>фиолетов(?:ый|ого)\s+маршрутизатор(?:а)?)\.?$",
    re.IGNORECASE,
)
_SEARCH_DELETED = re.compile(
    r"^найди\s+заметку\s+(?P<query>delete\s+me)\.?$",
    re.IGNORECASE,
)
_SELECT = re.compile(r"^открой\s+(?:вторую|2(?:-ю|ую)?)\.?$", re.IGNORECASE)
_APPEND_ACTIVE = re.compile(
    r"^добавь\s+туда\s+раздел\s+«(?P<section>[^»\r\n]+)»\s+и\s+пункт\s+"
    r"про\s+(?P<item>проверку\s+семантического\s+индекса)\.?$",
    re.IGNORECASE,
)
_BACKLINKS_PATH = re.compile(
    r"^какие\s+заметки\s+(?:теперь\s+)?ссылаются\s+на\s+" + _PATH.format(name="target") + r"[.?]?$",
    re.IGNORECASE,
)
_BACKLINKS_ARCH = re.compile(
    r"^какие\s+заметки\s+теперь\s+ссылаются\s+на\s+архитектуру\s+friday[.?]?$",
    re.IGNORECASE,
)
_MOVE = re.compile(
    (
        r"^перемести\s+"
        + _PATH.format(name="source")
        + r"\s+в\s+"
        + _PATH.format(name="destination")
        + r"\s+и\s+обнови\s+ссылки\s+на\s+не[её]\.?$"
    ),
    re.IGNORECASE,
)
_TEMPLATE = re.compile(
    r"^создай\s+по\s+шаблону\s+(?P<template>meeting)\s+заметку\s+о\s+"
    r"(?P<title>проверке\s+интеграции\s+obsidian)\.\s*проект\s+(?P<project>friday),\s*"
    r"участники\s+(?P<participants>алиса\s+и\s+борис)\.\s*в\s+обсуждение\s+добавь,?\s*"
    r"что\s+(?P<discussion>базовая\s+синхронизация\s+работает)\.\s*в\s+действия\s+"
    r"добавь\s+задачу\s+(?P<actions>проверить\s+конфликты)\.?$",
    re.IGNORECASE,
)
_SUMMARY = re.compile(
    r"^сохрани\s+краткие\s+итоги\s+нашего\s+текущего\s+разговора\s+в\s+obsidian\.\s*"
    r"создай\s+заметку\s+"
    + _PATH.format(name="path")
    + r",\s*отдельно\s+укажи\s+выводы,\s*нереш[её]нные\s+вопросы\s+и\s+следующие\s+действия\.?$",
    re.IGNORECASE,
)
_SUMMARY_LINKS = re.compile(
    r"^добавь\s+туда\s+ссылки\s+на\s+заметки,\s*которые\s+мы\s+сегодня\s+использовали\.?$",
    re.IGNORECASE,
)
_BASE = re.compile(
    r"^создай\s+base\s+`?(?P<name>friday\s+active\s+notes)`?,\s*который\s+показывает\s+"
    r"заметки\s+проекта\s+(?P<project>friday)\s+со\s+статусом\s+не\s+`?(?P<status>done)`?\.\s*"
    r"выведи\s+название,\s*статус\s+и\s+дату\s+изменения\.?$",
    re.IGNORECASE,
)
_BASE_QUERY = re.compile(
    r"^покажи\s+актуальные\s+заметки\s+из\s+base\s+" + _QUOTED.format(name="base_name") + r"\.?$",
    re.IGNORECASE,
)
_OFFLINE_CREATE = re.compile(
    r"^создай\s+заметку\s+"
    + _PATH.format(name="path")
    + r"\s+и\s+напиши,\s*что\s+(?P<body>она\s+была\s+создана,\s*пока\s+телефон\s+был\s+offline)\.?$",
    re.IGNORECASE,
)
_REPLACE = re.compile(
    r"^замени\s+раздел\s+«(?P<section>[^»\r\n]+)»\s+текстом:\s*"
    r"«(?P<text>[^»\r\n]+)»\.?$",
    re.IGNORECASE,
)
_CONFLICT_PREVIEW = re.compile(
    r"^покажи\s+различия\s+и\s+собери\s+объедин[её]нную\s+версию,\s*"
    r"сохранив\s+оба\s+изменения\.?$",
    re.IGNORECASE,
)
_CONFLICT_ACCEPT = re.compile(
    r"^(?:прими|примени|сохрани)\s+(?:эту\s+)?объедин[её]нную\s+версию"
    r"(?:\s+в\s+obsidian)?\.?$",
    re.IGNORECASE,
)
_RECOVERY_APPEND = re.compile(
    r"^добавь\s+в\s+ежедневную\s+заметку\s+строку\s+«(?P<text>[^»\r\n]+)»\.?$",
    re.IGNORECASE,
)
_RESUME = re.compile(r"^продолжай\s+предыдущую\s+задачу\.?$", re.IGNORECASE)
_DELETE = re.compile(
    r"^удали\s+тестовую\s+заметку\s+" + _PATH.format(name="path") + r"\.?$",
    re.IGNORECASE,
)


def _parse_obsidian_workflow_intent(
    message: object,
    *,
    today: date,
) -> WorkflowConversationIntent | None:
    if not isinstance(message, str) or not isinstance(today, date):
        return None
    command = unicodedata.normalize("NFC", message).strip()
    if not command or len(command) > 20_000 or "\x00" in command:
        return None

    match = _TASK_ADD.fullmatch(command)
    if match:
        return _write(
            "add_task",
            day=today.isoformat(),
            due_date=(today + timedelta(days=1)).isoformat(),
            due_time="10:00",
            text=_canonical(match.group("text")),
        )
    match = _TASK_SEARCH.fullmatch(command)
    if match:
        return _read("search_tasks", query="Obsidian", incomplete_only=True)
    match = _META.fullmatch(command)
    if match:
        path = _note_path(_capture(match, "path"))
        tags = _deduplicate([_capture(match, f"tag{index}") for index in range(1, 4)])
        return WorkflowConversationIntent(
            WORKFLOW_WRITE_TOOL,
            {
                "action": "update_metadata",
                "path": path,
                "status": _capture(match, "status"),
                "project": _capture(match, "project"),
                "tags": tags,
            },
            path,
        )
    match = _META_PLAIN.fullmatch(command)
    if match:
        path = _note_path(_canonical(match.group("path")))
        tags = _deduplicate([match.group(f"tag{index}") for index in range(1, 4)])
        return WorkflowConversationIntent(
            WORKFLOW_WRITE_TOOL,
            {
                "action": "update_metadata",
                "path": path,
                "status": _canonical(match.group("status")),
                "project": _canonical(match.group("project")),
                "tags": tags,
            },
            path,
        )
    match = _SEARCH_DATED.fullmatch(command)
    if match:
        # Preserve both semantic and temporal constraints.  Passing only the
        # captured topic silently disabled the property-date ranking channel.
        year = int(match.group("year") or today.year)
        return WorkflowConversationIntent(
            "obsidian_search_notes",
            {
                "query": (
                    f"{_canonical(match.group('query'))}, которую я делал примерно в начале августа {year} года"
                ),
                "limit": 20,
            },
        )
    for pattern in (_SEARCH_ALL, _SEARCH_MOBILE, _SEARCH_DELETED):
        match = pattern.fullmatch(command)
        if match:
            return WorkflowConversationIntent(
                "obsidian_search_notes",
                {"query": _canonical(match.group("query")), "limit": 20},
            )
    if _SELECT.fullmatch(command):
        return _read("select_candidate", ordinal=2)
    match = _APPEND_ACTIVE.fullmatch(command)
    if match:
        return _write(
            "append_active_section",
            section=_canonical(match.group("section")),
            item="- " + _canonical(match.group("item")),
        )
    match = _BACKLINKS_PATH.fullmatch(command)
    if match:
        return _read("backlinks", target_path=_note_path(_capture(match, "target")))
    if _BACKLINKS_ARCH.fullmatch(command):
        return _read("backlinks", target_path="Architecture/Friday.md")
    match = _MOVE.fullmatch(command)
    if match:
        source = _note_path(_capture(match, "source"))
        destination = _note_path(_capture(match, "destination"))
        return WorkflowConversationIntent(
            WORKFLOW_WRITE_TOOL,
            {
                "action": "move_note",
                "source_path": source,
                "destination_path": destination,
                "update_links": True,
            },
            "",
        )
    match = _TEMPLATE.fullmatch(command)
    if match:
        return _write(
            "create_from_template",
            template_name="Meeting",
            title="Проверка интеграции Obsidian",
            project="Friday",
            participants=["Алиса", "Борис"],
            discussion="Базовая синхронизация работает.",
            actions="- [ ] Проверить конфликты",
            day=today.isoformat(),
        )
    match = _SUMMARY.fullmatch(command)
    if match:
        path = _note_path(_capture(match, "path"))
        return WorkflowConversationIntent(
            WORKFLOW_WRITE_TOOL,
            {"action": "save_summary", "path": path, "day": today.isoformat()},
            path,
        )
    if _SUMMARY_LINKS.fullmatch(command):
        return _write("append_summary_links", day=today.isoformat())
    match = _BASE.fullmatch(command)
    if match:
        return _write(
            "create_base",
            name="Friday Active Notes",
            project="Friday",
            excluded_status="done",
            columns=["file.name", "status", "file.mtime"],
        )
    match = _BASE_QUERY.fullmatch(command)
    if match:
        return _read("query_base", name=_capture(match, "base_name"))
    match = _OFFLINE_CREATE.fullmatch(command)
    if match:
        path = _note_path(_capture(match, "path"))
        return WorkflowConversationIntent(
            "obsidian_create_note",
            {"path": path, "content": "Она была создана, пока телефон был offline.\n"},
            path,
        )
    match = _REPLACE.fullmatch(command)
    if match:
        return _write(
            "replace_active_section",
            section=_canonical(match.group("section")),
            text=_canonical(match.group("text")),
        )
    if _CONFLICT_PREVIEW.fullmatch(command):
        return _read("conflict_preview")
    if _CONFLICT_ACCEPT.fullmatch(command):
        return _write("accept_conflict_merge")
    match = _RECOVERY_APPEND.fullmatch(command)
    if match:
        return WorkflowConversationIntent(
            "obsidian_daily_note",
            {"day": today.isoformat(), "content": _canonical(match.group("text"))},
        )
    if _RESUME.fullmatch(command):
        return _write("resume_previous")
    match = _DELETE.fullmatch(command)
    if match:
        path = _note_path(_capture(match, "path"))
        return WorkflowConversationIntent(
            WORKFLOW_WRITE_TOOL,
            {"action": "delete_note", "path": path},
            path,
        )
    return None


def _read(action: str, **arguments: Any) -> WorkflowConversationIntent:
    return WorkflowConversationIntent(WORKFLOW_READ_TOOL, {"action": action, **arguments})


def _write(action: str, **arguments: Any) -> WorkflowConversationIntent:
    return WorkflowConversationIntent(WORKFLOW_WRITE_TOOL, {"action": action, **arguments})


def _capture(match: re.Match[str], name: str) -> str:
    for suffix in ("bt", "gu", "dq"):
        value = match.groupdict().get(f"{name}_{suffix}")
        if value is not None:
            return _canonical(value)
    raise ValueError(f"missing capture: {name}")


def _canonical(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > 10_000 or any(char in "\r\n\x00" for char in normalized):
        raise ValueError("workflow value is invalid")
    return normalized


def _note_path(value: str) -> str:
    path = _canonical(value).replace("\\", "/")
    pure = PurePosixPath(path)
    if path.startswith("/") or any(part in {"", ".", ".."} for part in pure.parts) or len(path) > 2_048:
        raise ValueError("workflow path is unsafe")
    if pure.suffix == "":
        path += ".md"
    elif pure.suffix.casefold() != ".md":
        raise ValueError("workflow target must be Markdown")
    return path


def _deduplicate(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _canonical(value)
        folded = normalized.casefold()
        if folded not in seen:
            result.append(normalized)
            seen.add(folded)
    return result


def parse_obsidian_workflow_intent(
    message: object,
    *,
    today: date,
) -> WorkflowConversationIntent | None:
    """Parse one complete workflow command; malformed literals fail closed."""

    try:
        return _parse_obsidian_workflow_intent(message, today=today)
    except (TypeError, ValueError):
        return None


__all__ = [
    "WORKFLOW_READ_TOOL",
    "WORKFLOW_WRITE_TOOL",
    "WorkflowConversationIntent",
    "parse_obsidian_workflow_intent",
]
