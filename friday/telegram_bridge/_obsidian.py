"""Closed Telegram presentation for the Obsidian onboarding state machine.

The backend owns state and wording.  This module owns the Telegram transport
contract: the complete Friday Device ID is copyable and visible, the HTTPS
guide remains a fallback, and callbacks carry only bounded opaque references.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit

from friday.telegram_bridge._base import CALLBACK_TARGET_RE

_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,256}$")
_CALLBACK_DATA_LIMIT_BYTES = 64


def _one_line(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if any(unicodedata.category(character).startswith("C") for character in text):
        return ""
    return text[:limit]


def _server_device_id(value: Any) -> str:
    candidate = str(value or "").strip()
    return candidate if _DEVICE_ID_RE.fullmatch(candidate) else ""


def _https_setup_url(value: Any) -> str:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 2_048 or "\\" in candidate:
        return ""
    if any(character.isspace() or unicodedata.category(character).startswith("C") for character in candidate):
        return ""
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ""
    return candidate


def _bounded_callback_data(action: str, target: str) -> str:
    if not CALLBACK_TARGET_RE.fullmatch(target):
        return ""
    callback_data = f"obs:{action}:{target}"
    return callback_data if len(callback_data.encode("utf-8")) <= _CALLBACK_DATA_LIMIT_BYTES else ""


def obsidian_panel(response: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Render one backend-owned onboarding snapshot without inventing progress."""

    server_device_id = _server_device_id(response.get("server_device_id"))
    setup_url = _https_setup_url(response.get("setup_url"))
    raw_vault = response.get("vault")
    vault: dict[str, Any] = raw_vault if isinstance(raw_vault, dict) else {}
    open_url = _https_setup_url(vault.get("open_url"))
    message = str(response.get("message") or "Настройка Obsidian продолжается.").strip()
    message = message[:2_800]
    sections = [message]
    if server_device_id:
        # Telegram renders this as selectable monospace text.  It is deliberately
        # also present outside the button: old clients may ignore `copy_text`.
        sections.append(f"Friday Device ID:\n`{server_device_id}`")
    if setup_url:
        sections.append(f"Если кнопка копирования не работает, откройте инструкцию:\n{setup_url}")
    if str(response.get("state") or "") in {
        "awaiting_obsidian_vault_registration",
        "round_trip_verification",
        "ready",
    }:
        sections.append(
            "Если имя vault в Obsidian отличается от Friday, задайте его командой /obsidian_alias точное имя"
        )
    raw_operations = response.get("operations")
    operations = raw_operations if isinstance(raw_operations, list) else []
    operation_lines: list[str] = []
    for raw_operation in operations[:5]:
        if not isinstance(raw_operation, dict):
            continue
        operation_id = _one_line(raw_operation.get("operation_id"), limit=200)
        work_item_id = _one_line(raw_operation.get("work_item_id"), limit=200)
        path = _one_line(raw_operation.get("path"), limit=512)
        status = _one_line(raw_operation.get("status"), limit=64)
        if not operation_id or not status:
            continue
        target = path or _one_line(raw_operation.get("method"), limit=64) or "операция"
        scan = "готов" if raw_operation.get("server_scan_complete") is True else "ожидается"
        android = "доставлено" if raw_operation.get("android_received") is True else "ожидается"
        work_item = f"; Work Item {work_item_id}" if work_item_id else ""
        operation_lines.append(
            f"- {target}: {status}; server scan — {scan}; Android — {android}; ID {operation_id}{work_item}"
        )
    if operation_lines:
        sections.append("Последние операции:\n" + "\n".join(operation_lines))

    raw_conflicts = response.get("conflicts")
    conflicts = raw_conflicts if isinstance(raw_conflicts, list) else []
    conflict_lines: list[str] = []
    for raw_conflict in conflicts[:5]:
        if not isinstance(raw_conflict, dict):
            continue
        conflict_id = _one_line(raw_conflict.get("id"), limit=200)
        canonical_path = _one_line(raw_conflict.get("canonical_path"), limit=512)
        artifact_path = _one_line(raw_conflict.get("conflict_path"), limit=512)
        if not conflict_id or not canonical_path or not artifact_path:
            continue
        conflict_lines.append(
            f"- Основная заметка: {canonical_path}\n  Конфликтная копия: {artifact_path}\n  ID: {conflict_id}"
        )
    if conflict_lines:
        raw_count = response.get("conflict_count")
        count = (
            raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else len(conflicts)
        )
        count = max(len(conflict_lines), count)
        sections.append(
            f"Открытые конфликты: {count}. Обе версии сохранены; конфликтные копии "
            "не удаляются автоматически.\n" + "\n".join(conflict_lines)
        )

    keyboard: list[list[dict[str, Any]]] = []
    if server_device_id:
        # Bot API contract: copy_text belongs to the first button of the first
        # row.  There is no callback and therefore no false "copied" receipt.
        keyboard.append(
            [
                {
                    "text": "Скопировать Friday Device ID",
                    "copy_text": {"text": server_device_id},
                }
            ]
        )
    if setup_url:
        keyboard.append([{"text": "Открыть пошаговую инструкцию", "url": setup_url}])

    raw_candidates = response.get("candidates")
    candidates = raw_candidates if isinstance(raw_candidates, list) else []
    for raw_candidate in candidates[:8]:
        if not isinstance(raw_candidate, dict):
            continue
        candidate_id = str(raw_candidate.get("id") or "")
        if candidate_id == "current":
            continue
        callback_data = _bounded_callback_data("select", candidate_id)
        if not callback_data:
            continue
        display_name = _one_line(raw_candidate.get("display_name"), limit=40) or "Android-устройство"
        short_suffix = _one_line(raw_candidate.get("short_suffix"), limit=16)
        label = f"{display_name} · {short_suffix}" if short_suffix else display_name
        keyboard.append([{"text": label, "callback_data": f"obs:select:{candidate_id}"}])

    raw_actions = response.get("actions")
    actions = (
        {str(item) for item in raw_actions if isinstance(item, str)}
        if isinstance(raw_actions, list)
        else set()
    )
    if "check" in actions:
        keyboard.append([{"text": "Проверить подключение", "callback_data": "obs:check:current"}])
    if "open_test_note" in actions and open_url:
        keyboard.append([{"text": "Открыть тестовую заметку в Obsidian", "url": open_url}])
    if actions & {"opened", "confirm_open", "confirm-open"}:
        keyboard.append([{"text": "Тестовая заметка открылась", "callback_data": "obs:opened:current"}])
    if "retry" in actions:
        keyboard.append([{"text": "Повторить шаг", "callback_data": "obs:retry:current"}])
    if "cancel" in actions:
        keyboard.append([{"text": "Отменить настройку", "callback_data": "obs:cancel:current"}])

    return "\n\n".join(section for section in sections if section), (
        {"inline_keyboard": keyboard} if keyboard else None
    )


__all__ = ["obsidian_panel"]
