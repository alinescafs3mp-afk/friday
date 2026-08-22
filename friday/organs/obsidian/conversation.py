"""Fail-closed conversational boundary for native Obsidian tools.

The model is deliberately not an authority for selecting an Obsidian effect,
inventing an idempotency key, or wording a delivery receipt.  This module only
accepts closed, direct requests found in the *current* user text.  It also turns
trusted runtime dictionaries into bounded, code-owned text after validating the
exact result contract.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Any

from friday.audit_privacy import decode_audit_privacy_key

OBSIDIAN_READ_TOOL_NAMES = frozenset(
    {
        "obsidian_list_vaults",
        "obsidian_list_notes",
        "obsidian_search_notes",
        "obsidian_read_note",
    }
)
OBSIDIAN_WRITE_TOOL_NAMES = frozenset(
    {
        "obsidian_create_note",
        "obsidian_append_note",
        "obsidian_set_properties",
        "obsidian_daily_note",
    }
)
OBSIDIAN_TOOL_NAMES = OBSIDIAN_READ_TOOL_NAMES | OBSIDIAN_WRITE_TOOL_NAMES

_MAX_MESSAGE_CHARS = 20_000
_MAX_NOTE_PATH_CHARS = 2_048
_MAX_NOTE_TEXT_CHARS = 200_000
_MAX_RENDERED_BODY_CHARS = 12_000
_ROOT_MESSAGE = re.compile(r"msg_[0-9a-f]{16}")
_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,199}")
_REVISION = re.compile(r"[0-9a-f]{64}")
_VAULT_ID = re.compile(r"obsvault_[0-9a-f]{16}")
_OPERATION_DOMAIN = b"friday.obsidian-conversation-operation.v1\0"
_INTERNAL_VAULT_ROOTS = frozenset({".stfolder", ".stignore", ".stversions", ".trash"})
_VAULT_STATES = frozenset(
    {
        "provisioning",
        "offering_folder",
        "awaiting_folder_acceptance",
        "initial_sync",
        "awaiting_vault_registration",
        "verifying",
        "ready",
        "disconnected",
        "failed",
    }
)
_OPERATION_STATUSES = frozenset(
    {"committed", "reconciled", "scan_pending", "scan_complete", "delivery_pending", "delivered"}
)
_SEARCH_CHANNELS = frozenset({"exact_path", "exact_title", "path_title", "lexical"})
_METHOD_BY_TOOL = {
    "obsidian_create_note": "create",
    "obsidian_append_note": "append",
    "obsidian_set_properties": "set_properties",
    "obsidian_daily_note": "daily_note",
}

_REFUSE_AMBIGUOUS = (
    "Не удалось однозначно разобрать прямую команду Obsidian; укажите действие, путь и данные явно."
)

_QUOTED_SPAN = re.compile(
    r"```.*?```|`[^`\r\n]*`|«[^»\r\n]*»|“[^”\r\n]*”|\"[^\"\r\n]*\"",
    re.DOTALL,
)
_ACTION = re.compile(
    r"\b(?:покажи|перечисли|выведи|найди|поищи|прочитай|создай|создавай|создать|"
    r"добавь|добавляй|добавить|установи|устанавливай|установить|измени|изменить|"
    r"obsidian_(?:list_vaults|list_notes|search_notes|read_note|create_note|"
    r"append_note|set_properties|daily_note))\b",
    re.IGNORECASE,
)
_META = re.compile(
    r"(?:\b(?:пример|фраза|цитат[аыуе]?|пересказ|шаблон|формулировк[ауы]|"
    r"мета[- ]?инструкци[яию]|тестов(?:ая|ый)\s+фраза)\b|"
    r"^\s*как\s+(?:показать|найти|прочитать|создать|добавить|установить|изменить)\b|"
    r"\b(?:объясни|расскажи|покажи|напиши|приведи)\b.{0,80}\bкак\b|"
    r"\b(?:он|она|они|пользователь|ассистент|пятница|сообщение)\b.{0,50}"
    r"\b(?:сказал[аи]?|написал[аи]?|просил[аи]?)\b|"
    r"\bчто\s+(?:делает|означает)\s+obsidian_)",
    re.IGNORECASE | re.DOTALL,
)
_NEGATED = re.compile(
    r"(?:\bне\s+(?:надо\s+|нужно\s+|следует\s+|хочу\s+)?"
    r"(?:показывай|показывать|ищи|искать|читай|читать|создавай|создавать|создать|"
    r"добавляй|добавлять|добавить|устанавливай|устанавливать|установить|"
    r"изменяй|изменять|трогай)|"
    r"\bбез\s+(?:показа|поиска|чтения|создания|добавления|изменения)|"
    r"\b(?:отмени|забудь|игнорируй)\b)",
    re.IGNORECASE,
)
_ATTACHMENT_DERIVED = re.compile(
    r"(?:\b(?:этот|этого|эту|это|эти|данный|данного|прикрепл[её]нный)\s+"
    r"(?:файл|файла|документ|документа|вложение|вложения)|"
    r"\b(?:из|по)\s+(?:файлу|файла|документу|документа|вложению|вложения)|"
    r"\bво?\s+вложении\b|\bприкрепл[её]нн(?:ый|ого|ую|ое)\b)",
    re.IGNORECASE,
)

_POLITE_PREFIX = re.compile(
    r"^(?:(?:пожалуйста|пятниц[аы])\s*[,!:]?\s*)+",
    re.IGNORECASE,
)
_LIST_VAULTS = re.compile(
    r"^(?:покажи|перечисли|выведи)\s+(?:мои\s+)?(?:подключ[её]нные\s+)?"
    r"(?:хранилища|vaults?|vaultы)\s+(?:в\s+)?obsidian\.?$",
    re.IGNORECASE,
)
_LIST_NOTES = re.compile(
    r"^(?:покажи|перечисли|выведи)\s+(?:список\s+)?замет(?:ок|ки)\s+"
    r"(?:в\s+)?obsidian\.?$",
    re.IGNORECASE,
)
_SEARCH = re.compile(
    r"^(?:найди|поищи)\s+в\s+obsidian\s+(?:замет(?:ку|ки|ок)\s+)?"
    r"(?:(?:по\s+запросу|про|об|о)\s+)?(?P<query>`[^`\r\n]+`|"
    r"«[^»\r\n]+»|\"[^\"\r\n]+\"|[^?!\r\n]{1,1000}?)\.?$",
    re.IGNORECASE,
)
_READ = re.compile(
    r"^(?:прочитай|покажи\s+содержимое)\s+в\s+obsidian\s+заметку\s+"
    r"(?P<path>`[^`\r\n]+`|«[^»\r\n]+»|\"[^\"\r\n]+\"|.+?\.md)\.?$",
    re.IGNORECASE,
)
_READ_TRAILING_CHANNEL = re.compile(
    r"^(?:прочитай|покажи\s+содержимое)\s+замет(?:ку|ки)\s+"
    r"(?P<path>`[^`\r\n]+`|«[^»\r\n]+»|\"[^\"\r\n]+\"|.+?\.md)\s+"
    r"(?:из|в)\s+obsidian\.?$",
    re.IGNORECASE,
)
_CREATE_EXACT = re.compile(
    r"^создай\s+в\s+obsidian\s+заметку\s+(?P<path>.+?\.md)\.\s*"
    r"заголовок:\s*«(?P<heading>[^»\r\n]{1,200})»\.\s*"
    r"внутри\s+напиши,\s+что\s+(?P<body>[^,\r\n]{1,2000}),\s+"
    r"и\s+добавь\s+текущую\s+дату\.?$",
    re.IGNORECASE,
)
_CREATE_WITH_TEXT = re.compile(
    r"^создай\s+в\s+obsidian\s+заметку\s+"
    r"(?P<path>`[^`\r\n]+`|«[^»\r\n]+»|\"[^\"\r\n]+\"|.+?\.md)\s+"
    r"с\s+(?:текстом|содержимым)\s*:?\s*"
    r"(?P<content>`[^`\r\n]+`|«[^»\r\n]+»|\"[^\"\r\n]+\")\.?$",
    re.IGNORECASE,
)
_APPEND = re.compile(
    r"^добавь\s+в\s+obsidian\s+(?:в\s+)?заметку\s+"
    r"(?P<path>`[^`\r\n]+`|«[^»\r\n]+»|\"[^\"\r\n]+\")\s+"
    r"(?:текст|запись)\s*:?\s*(?P<text>`[^`\r\n]+`|«[^»\r\n]+»|"
    r"\"[^\"\r\n]+\")\.?$",
    re.IGNORECASE,
)
_SET_PROPERTY = re.compile(
    r"^установи\s+в\s+obsidian\s+у\s+заметки\s+"
    r"(?P<path>`[^`\r\n]+`|«[^»\r\n]+»|\"[^\"\r\n]+\")\s+"
    r"свойство\s+(?P<key>`[^`\r\n]+`|«[^»\r\n]+»|\"[^\"\r\n]+\")\s+"
    r"(?:в|=)\s*(?P<value>`[^`\r\n]*`|«[^»\r\n]*»|\"[^\"\r\n]*\")\.?$",
    re.IGNORECASE,
)
_DAILY = re.compile(
    r"^добавь\s+(?:в\s+obsidian\s+в\s+(?:сегодняшнюю\s+)?ежедневную\s+"
    r"заметку|в\s+(?:сегодняшнюю\s+)?ежедневную\s+заметку\s+obsidian)\s+"
    r"(?:текст|запись)\s*:?\s*(?P<text>`[^`\r\n]+`|«[^»\r\n]+»|"
    r"\"[^\"\r\n]+\")\.?$",
    re.IGNORECASE,
)

_OPERATION_MARKER = re.compile(
    r'<!-- friday:(?:create|append) operation="[0-9a-f]{64}" '
    r'arguments="[0-9a-f]{64}" -->'
)
_SAFE_UNICODE_FORMAT_CHARACTERS = frozenset({"\u200c", "\u200d"})


def _has_unsafe_unicode_control(value: str, *, multiline: bool) -> bool:
    """Keep structural controls closed while allowing joiners used by real text."""

    for character in value:
        if character in _SAFE_UNICODE_FORMAT_CHARACTERS:
            continue
        if multiline and character in "\r\n\t":
            continue
        if unicodedata.category(character).startswith("C"):
            return True
    return False


@dataclass(frozen=True, slots=True)
class ObsidianConversationIntent:
    """A direct current-message intent, or a code-owned reason to refuse it."""

    tool_name: str
    explicit_path: str = ""
    direct_arguments: Mapping[str, Any] | None = None
    error: str = ""


def _refusal(reason: str) -> ObsidianConversationIntent:
    return ObsidianConversationIntent(tool_name="", error=reason)


def _mask_quoted(text: str) -> str:
    return _QUOTED_SPAN.sub(lambda match: " " * len(match.group(0)), text)


def _unquote(value: str) -> str:
    text = value.strip()
    pairs = {"`": "`", "«": "»", '"': '"', "“": "”"}
    if len(text) >= 2 and text[0] in pairs and text[-1] == pairs[text[0]]:
        text = text[1:-1].strip()
    return unicodedata.normalize("NFC", text)


def _today(value: date | None) -> date:
    selected = date.today() if value is None else value
    if not isinstance(selected, date) or isinstance(selected, datetime):
        raise TypeError("today must be a local date")
    return selected


def _validate_path(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("note path must be text")
    raw = value.strip()
    if not raw or len(raw) > _MAX_NOTE_PATH_CHARS:
        raise ValueError("note path is empty or too long")
    try:
        raw.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("note path must be valid UTF-8") from exc
    path = unicodedata.normalize("NFC", raw)
    if len(path) > _MAX_NOTE_PATH_CHARS:
        raise ValueError("note path is empty or too long")
    if "\x00" in path or "\\" in path or path.startswith("/"):
        raise ValueError("note path is not a relative POSIX path")
    if re.match(r"^[A-Za-z]:", path):
        raise ValueError("note path is not a relative POSIX path")
    if _has_unsafe_unicode_control(path, multiline=False):
        raise ValueError("note path contains a control character")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts) or len(parts) - 1 > 32:
        raise ValueError("note path contains an unsafe segment")
    folded = tuple(part.casefold() for part in parts)
    if folded[0] in _INTERNAL_VAULT_ROOTS or ".obsidian" in folded:
        raise ValueError("note path enters a reserved directory")
    if ".sync-conflict-" in folded[-1]:
        raise ValueError("note path names a conflict copy")
    pure = PurePosixPath(*parts)
    if pure.suffix.casefold() != ".md" or not pure.name:
        raise ValueError("note path must name a Markdown note")
    return path


def _bounded_current_text(message: object) -> str | None:
    if not isinstance(message, str):
        return None
    text = unicodedata.normalize("NFC", message).strip()
    if not text:
        return None
    if len(text) > _MAX_MESSAGE_CHARS or "\x00" in text:
        if "obsidian" in text.casefold() and _ACTION.search(text):
            return ""
        return None
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return "" if "obsidian" in text.casefold() else None
    return text


def obsidian_conversation_intent(
    message: object,
    *,
    today: date | None = None,
) -> ObsidianConversationIntent | None:
    """Parse one direct current-text request without consulting history or files.

    ``None`` means that the message is not an Obsidian command.  A returned
    intent with ``error`` is an explicit, code-owned refusal and must never be
    sent to a tool or completed by the model.
    """

    text = _bounded_current_text(message)
    if text is None:
        return None
    if not text:
        return _refusal(_REFUSE_AMBIGUOUS)
    if "obsidian" not in text.casefold() or _ACTION.search(text) is None:
        return None

    authority = _mask_quoted(text)
    if (
        text.lstrip().startswith(">")
        or (_ACTION.search(text) is not None and _ACTION.search(authority) is None)
        or _META.search(authority) is not None
    ):
        return None
    if _NEGATED.search(authority) is not None:
        return None
    if _ATTACHMENT_DERIVED.search(authority) is not None:
        return None

    command = _POLITE_PREFIX.sub("", text).strip()
    if _LIST_VAULTS.fullmatch(command):
        return ObsidianConversationIntent("obsidian_list_vaults", direct_arguments={})
    if _LIST_NOTES.fullmatch(command):
        return ObsidianConversationIntent("obsidian_list_notes", direct_arguments={})

    match = _SEARCH.fullmatch(command)
    if match is not None:
        query = _unquote(match.group("query"))
        if not query or len(query) > 1_000:
            return _refusal(_REFUSE_AMBIGUOUS)
        return ObsidianConversationIntent(
            "obsidian_search_notes",
            direct_arguments={"query": query, "limit": 20},
        )

    match = _READ.fullmatch(command) or _READ_TRAILING_CHANNEL.fullmatch(command)
    if match is not None:
        try:
            path = _validate_path(_unquote(match.group("path")))
        except (TypeError, ValueError):
            return _refusal(_REFUSE_AMBIGUOUS)
        return ObsidianConversationIntent(
            "obsidian_read_note",
            explicit_path=path,
            direct_arguments={"path": path},
        )

    match = _CREATE_EXACT.fullmatch(command)
    if match is not None:
        try:
            path = _validate_path(_unquote(match.group("path")))
            selected_day = _today(today)
        except (TypeError, ValueError):
            return _refusal(_REFUSE_AMBIGUOUS)
        heading = unicodedata.normalize("NFC", match.group("heading").strip())
        body = unicodedata.normalize("NFC", match.group("body").strip())
        if not heading or not body or any(character in "\r\n\x00" for character in heading + body):
            return _refusal(_REFUSE_AMBIGUOUS)
        sentence = body[0].upper() + body[1:]
        if sentence[-1] not in ".!?":
            sentence += "."
        content = f"# {heading}\n\n{sentence}\n\n{selected_day.isoformat()}\n"
        if len(content) > _MAX_NOTE_TEXT_CHARS:
            return _refusal(_REFUSE_AMBIGUOUS)
        return ObsidianConversationIntent(
            "obsidian_create_note",
            explicit_path=path,
            direct_arguments={"path": path, "content": content},
        )

    match = _CREATE_WITH_TEXT.fullmatch(command)
    if match is not None:
        try:
            path = _validate_path(_unquote(match.group("path")))
        except (TypeError, ValueError):
            return _refusal(_REFUSE_AMBIGUOUS)
        content = _unquote(match.group("content"))
        if not content or len(content) > _MAX_NOTE_TEXT_CHARS or "\x00" in content:
            return _refusal(_REFUSE_AMBIGUOUS)
        return ObsidianConversationIntent(
            "obsidian_create_note",
            explicit_path=path,
            direct_arguments={"path": path, "content": content},
        )

    match = _APPEND.fullmatch(command)
    if match is not None:
        try:
            path = _validate_path(_unquote(match.group("path")))
        except (TypeError, ValueError):
            return _refusal(_REFUSE_AMBIGUOUS)
        addition = _unquote(match.group("text"))
        if not addition or len(addition) > _MAX_NOTE_TEXT_CHARS:
            return _refusal(_REFUSE_AMBIGUOUS)
        return ObsidianConversationIntent(
            "obsidian_append_note",
            explicit_path=path,
            direct_arguments={"path": path, "text": addition},
        )

    match = _SET_PROPERTY.fullmatch(command)
    if match is not None:
        try:
            path = _validate_path(_unquote(match.group("path")))
        except (TypeError, ValueError):
            return _refusal(_REFUSE_AMBIGUOUS)
        key = _unquote(match.group("key"))
        value = _unquote(match.group("value"))
        if (
            not key
            or len(key) > 200
            or any(character in "\r\n\x00" for character in key)
            or len(value) > 10_000
        ):
            return _refusal(_REFUSE_AMBIGUOUS)
        return ObsidianConversationIntent(
            "obsidian_set_properties",
            explicit_path=path,
            direct_arguments={"path": path, "properties": {key: value}},
        )

    match = _DAILY.fullmatch(command)
    if match is not None:
        try:
            selected_day = _today(today)
        except TypeError:
            return _refusal(_REFUSE_AMBIGUOUS)
        addition = _unquote(match.group("text"))
        if not addition or len(addition) > _MAX_NOTE_TEXT_CHARS:
            return _refusal(_REFUSE_AMBIGUOUS)
        return ObsidianConversationIntent(
            "obsidian_daily_note",
            direct_arguments={"day": selected_day.isoformat(), "content": addition},
        )

    return _refusal(_REFUSE_AMBIGUOUS)


def obsidian_operation_id(
    storage: Any,
    owner_id: object,
    root_user_message_id: object,
    tool_name: object,
) -> str:
    """Derive one installation-local idempotency key from root message lineage."""

    if not isinstance(owner_id, str) or _USER_ID.fullmatch(owner_id) is None:
        raise ValueError("invalid Obsidian operation owner")
    if not isinstance(root_user_message_id, str) or _ROOT_MESSAGE.fullmatch(root_user_message_id) is None:
        raise ValueError("invalid root user message lineage")
    if not isinstance(tool_name, str) or tool_name not in OBSIDIAN_WRITE_TOOL_NAMES:
        raise ValueError("operation IDs are only defined for shipped Obsidian write tools")
    try:
        row = storage.execute("SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'").fetchone()
        key = decode_audit_privacy_key(row[0] if row is not None else None)
    except Exception as exc:  # noqa: BLE001 - missing installation proof must fail closed
        raise RuntimeError("Obsidian operation signing key is unavailable") from exc
    payload = b"\0".join(
        (
            owner_id.encode("ascii"),
            root_user_message_id.encode("ascii"),
            tool_name.encode("ascii"),
        )
    )
    digest = hmac.new(key, _OPERATION_DOMAIN + payload, hashlib.sha256).hexdigest()
    return f"obsop_{digest}"


def _strict_object(value: object, fields: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("result fields do not match the contract")
    return value


def _strict_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("Boolean result field has the wrong type")
    return value


def _strict_int(value: object, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("integer result field is outside its contract")
    return value


def _line(value: object, *, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value):
        raise ValueError("text result field is outside its contract")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("text result field is not valid UTF-8") from exc
    normalized = unicodedata.normalize("NFC", value)
    if len(normalized) > maximum or (not allow_empty and not normalized):
        raise ValueError("text result field is outside its contract")
    if _has_unsafe_unicode_control(normalized, multiline=False):
        raise ValueError("text result field contains a control character")
    return normalized


def _multiline(value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ValueError("multiline result field is outside its contract")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("multiline result field is not valid UTF-8") from exc
    if _has_unsafe_unicode_control(value, multiline=True):
        raise ValueError("multiline result field contains an unsafe control character")
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _timestamp(value: object) -> str:
    text = _line(value, maximum=64)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp is missing a timezone")
    return text


def _revision(value: object) -> str:
    if not isinstance(value, str) or _REVISION.fullmatch(value) is None:
        raise ValueError("invalid note revision")
    return value


def _size(value: object) -> int:
    return _strict_int(value, minimum=0, maximum=4 * 1024 * 1024)


def _typed_properties(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or len(value) > 100:
        raise ValueError("invalid note properties")
    result: dict[str, object] = {}
    for raw_key, raw_item in value.items():
        key = _line(raw_key, maximum=200)
        item = _strict_object(raw_item, frozenset({"type", "value"}))
        kind = item["type"]
        payload = item["value"]
        if kind == "text":
            payload = _multiline(payload, maximum=200_000)
        elif kind == "list":
            if not isinstance(payload, list) or len(payload) > 256:
                raise ValueError("invalid list property")
            payload = [_line(entry, maximum=10_000, allow_empty=True) for entry in payload]
        elif kind == "number":
            if (
                isinstance(payload, bool)
                or not isinstance(payload, (int, float))
                or not math.isfinite(float(payload))
            ):
                raise ValueError("invalid number property")
        elif kind == "checkbox":
            payload = _strict_bool(payload)
        elif kind == "date":
            if not isinstance(payload, str):
                raise ValueError("invalid date property")
            date.fromisoformat(payload)
        elif kind == "datetime":
            if not isinstance(payload, str):
                raise ValueError("invalid datetime property")
            datetime.fromisoformat(payload.replace("Z", "+00:00"))
        else:
            raise ValueError("unsupported property type")
        result[key] = {"type": kind, "value": payload}
    # Bound the aggregate before it reaches a chat response.
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False)) > 200_000:
        raise ValueError("note properties are too large")
    return result


def _summary(value: object) -> dict[str, object]:
    item = _strict_object(
        value,
        frozenset({"path", "title", "revision", "size_bytes", "modified_at"}),
    )
    return {
        "path": _validate_path(item["path"]),
        "title": _line(item["title"], maximum=1_000),
        "revision": _revision(item["revision"]),
        "size_bytes": _size(item["size_bytes"]),
        "modified_at": _timestamp(item["modified_at"]),
    }


def _render_vaults(data: object) -> str:
    result = _strict_object(data, frozenset({"vaults", "count"}))
    vaults = result["vaults"]
    if not isinstance(vaults, list) or len(vaults) > 8:
        raise ValueError("invalid vault list")
    count = _strict_int(result["count"], minimum=0, maximum=8)
    if count != len(vaults):
        raise ValueError("vault count does not match the result")
    lines = [f"Хранилища Obsidian: {count}."]
    for raw in vaults:
        item = _strict_object(raw, frozenset({"id", "name", "state", "android_alias"}))
        vault_id = _line(item["id"], maximum=80)
        if _VAULT_ID.fullmatch(vault_id) is None:
            raise ValueError("invalid vault id")
        name = _line(item["name"], maximum=200)
        alias = _line(item["android_alias"], maximum=100)
        state = item["state"]
        if not isinstance(state, str) or state not in _VAULT_STATES:
            raise ValueError("invalid vault state")
        lines.append(f"- {name} (Android: {alias}; состояние: {state}; id: {vault_id})")
    return "\n".join(lines)


def _render_notes(data: object) -> str:
    result = _strict_object(data, frozenset({"notes", "count"}))
    notes = result["notes"]
    if not isinstance(notes, list) or len(notes) > 1_000:
        raise ValueError("invalid note list")
    count = _strict_int(result["count"], minimum=0, maximum=1_000)
    if count != len(notes):
        raise ValueError("note count does not match the result")
    normalized = [_summary(item) for item in notes]
    paths = [str(item["path"]) for item in normalized]
    if len(paths) != len(set(paths)):
        raise ValueError("duplicate note paths")
    visible = normalized[:50]
    lines = [f"Заметки Obsidian: {count}. Показано: {len(visible)}."]
    for item in visible:
        lines.append(
            f"- {item['path']} — {item['title']} ({item['size_bytes']} байт; "
            f"изменена {item['modified_at']}; revision {item['revision']})"
        )
    return "\n".join(lines)


def _render_search(data: object) -> str:
    result = _strict_object(data, frozenset({"matches", "count"}))
    matches = result["matches"]
    if not isinstance(matches, list) or len(matches) > 100:
        raise ValueError("invalid search result list")
    count = _strict_int(result["count"], minimum=0, maximum=100)
    if count != len(matches):
        raise ValueError("search count does not match the result")
    normalized: list[dict[str, object]] = []
    for raw in matches:
        item = _strict_object(
            raw,
            frozenset({"path", "title", "revision", "modified_at", "excerpt", "score", "match_channels"}),
        )
        score = item["score"]
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
            or float(score) < 0
        ):
            raise ValueError("invalid search score")
        channels = item["match_channels"]
        if (
            not isinstance(channels, list)
            or not channels
            or len(channels) > len(_SEARCH_CHANNELS)
            or any(not isinstance(channel, str) or channel not in _SEARCH_CHANNELS for channel in channels)
            or len(channels) != len(set(channels))
        ):
            raise ValueError("invalid search channels")
        normalized.append(
            {
                "path": _validate_path(item["path"]),
                "title": _line(item["title"], maximum=1_000),
                "revision": _revision(item["revision"]),
                "modified_at": _timestamp(item["modified_at"]),
                "excerpt": _line(item["excerpt"], maximum=500, allow_empty=True),
                "score": float(score),
                "channels": ", ".join(channels),
            }
        )
    paths = [str(item["path"]) for item in normalized]
    if len(paths) != len(set(paths)):
        raise ValueError("duplicate search paths")
    visible = normalized[:20]
    lines = [f"Совпадения в Obsidian: {count}. Показано: {len(visible)}."]
    for item in visible:
        lines.append(
            f"- {item['path']} — {item['title']} [score {item['score']:g}; {item['channels']}]\n"
            f"  {item['excerpt']}"
        )
    return "\n".join(lines)


def _render_read(data: object, *, expected_path: str) -> str:
    item = _strict_object(
        data,
        frozenset(
            {"path", "title", "content", "body", "properties", "revision", "size_bytes", "modified_at"}
        ),
    )
    path = _validate_path(item["path"])
    if expected_path and path != _validate_path(expected_path):
        raise ValueError("read result path does not match the request")
    title = _line(item["title"], maximum=1_000)
    raw_content = item["content"]
    content = _multiline(raw_content, maximum=4 * 1024 * 1024)
    body = _multiline(item["body"], maximum=4 * 1024 * 1024)
    if body and body not in content:
        raise ValueError("read body is not part of note content")
    size = _size(item["size_bytes"])
    if not isinstance(raw_content, str) or size != len(raw_content.encode("utf-8")):
        raise ValueError("read size does not match note content")
    properties = _typed_properties(item["properties"])
    revision = _revision(item["revision"])
    modified_at = _timestamp(item["modified_at"])
    visible_body = _OPERATION_MARKER.sub("", body).strip()
    shortened = len(visible_body) > _MAX_RENDERED_BODY_CHARS
    if shortened:
        visible_body = visible_body[:_MAX_RENDERED_BODY_CHARS].rstrip()
    properties_json = json.dumps(properties, ensure_ascii=False, sort_keys=True, allow_nan=False)
    lines = [
        f"Заметка: {path}",
        f"Заголовок: {title}",
        f"Revision: {revision}",
        f"Изменена: {modified_at}",
        f"Свойства: {properties_json}",
        "",
        visible_body or "(пустая заметка)",
    ]
    if shortened:
        lines.append("\n… Текст сокращён для ответа; revision указана выше.")
    return "\n".join(lines)


def _render_mutation(
    tool_name: str,
    data: object,
    *,
    expected_operation_id: str,
    expected_path: str,
) -> str:
    item = _strict_object(
        data,
        frozenset(
            {
                "operation_id",
                "method",
                "status",
                "path",
                "revision",
                "previous_revision",
                "created",
                "applied",
                "replayed",
                "delivery",
            }
        ),
    )
    operation_id = _line(item["operation_id"], maximum=200)
    if expected_operation_id and operation_id != _line(expected_operation_id, maximum=200):
        raise ValueError("operation receipt does not match the request")
    if item["method"] != _METHOD_BY_TOOL[tool_name]:
        raise ValueError("operation method does not match the tool")
    status = item["status"]
    if not isinstance(status, str) or status not in _OPERATION_STATUSES:
        raise ValueError("unsupported successful operation status")
    path = _validate_path(item["path"])
    if expected_path and path != _validate_path(expected_path):
        raise ValueError("operation path does not match the request")
    revision = _revision(item["revision"])
    previous = item["previous_revision"]
    if previous is not None:
        previous = _revision(previous)
    created = _strict_bool(item["created"])
    applied = _strict_bool(item["applied"])
    replayed = _strict_bool(item["replayed"])
    if tool_name in {"obsidian_append_note", "obsidian_set_properties"} and created:
        raise ValueError("update receipt unexpectedly claims file creation")
    if tool_name == "obsidian_create_note" and not created:
        raise ValueError("create receipt does not prove file creation")

    delivery = _strict_object(
        item["delivery"],
        frozenset(
            {
                "local_write_complete",
                "server_scan_complete",
                "android_connected",
                "android_completion",
                "android_received",
                "obsidian_opened",
            }
        ),
    )
    local = _strict_bool(delivery["local_write_complete"])
    server_scan = _strict_bool(delivery["server_scan_complete"])
    android_connected = _strict_bool(delivery["android_connected"])
    android_received = _strict_bool(delivery["android_received"])
    opened = _strict_bool(delivery["obsidian_opened"])
    completion = delivery["android_completion"]
    if completion is not None and (
        isinstance(completion, bool)
        or not isinstance(completion, (int, float))
        or not math.isfinite(float(completion))
        or not 0 <= float(completion) <= 100
    ):
        raise ValueError("invalid Android completion")
    if not local or opened:
        raise ValueError("mutation receipt lacks a local proof or claims an unproven note opening")
    if status in {"scan_complete", "delivery_pending", "delivered"} and not server_scan:
        raise ValueError("operation status lacks server scan evidence")
    if status == "delivered" and not android_received:
        raise ValueError("delivered status lacks Android receipt evidence")
    if android_received and (not server_scan or status != "delivered"):
        raise ValueError("Android receipt facts are inconsistent")

    if replayed:
        result_line = "Результат ранее выполненной операции подтверждён; повторной записи не было."
    elif not applied:
        result_line = "Операция подтверждена; изменения файла не потребовались."
    else:
        result_line = {
            "obsidian_create_note": "Заметка создана в локальной серверной копии vault.",
            "obsidian_append_note": "Текст добавлен в локальную серверную копию заметки.",
            "obsidian_set_properties": "Свойства изменены в локальной серверной копии заметки.",
            "obsidian_daily_note": "Ежедневная заметка изменена в локальной серверной копии vault.",
        }[tool_name]
    completion_text = "нет подтверждённого значения" if completion is None else f"{float(completion):g}%"
    lines = [
        result_line,
        f"Путь: {path}",
        f"Revision: {revision}",
        f"Operation ID: {operation_id}",
        "Локальная запись: подтверждена.",
        f"Сканирование серверной копии: {'подтверждено' if server_scan else 'ожидается'}.",
        f"Android подключён: {'да' if android_connected else 'нет'}.",
        f"Получение этой revision на Android: {'подтверждено' if android_received else 'ожидается'}.",
        f"Прогресс Android: {completion_text}.",
    ]
    if previous is not None:
        lines.insert(3, f"Предыдущая revision: {previous}")
    return "\n".join(lines)


def render_obsidian_tool_result(
    tool_name: object,
    data: object,
    *,
    expected_operation_id: str = "",
    expected_path: str = "",
) -> str | None:
    """Validate and render one shipped tool result; malformed data returns ``None``."""

    if not isinstance(tool_name, str) or tool_name not in OBSIDIAN_TOOL_NAMES:
        return None
    try:
        if tool_name == "obsidian_list_vaults":
            return _render_vaults(data)
        if tool_name == "obsidian_list_notes":
            return _render_notes(data)
        if tool_name == "obsidian_search_notes":
            return _render_search(data)
        if tool_name == "obsidian_read_note":
            return _render_read(data, expected_path=expected_path)
        return _render_mutation(
            tool_name,
            data,
            expected_operation_id=expected_operation_id,
            expected_path=expected_path,
        )
    except Exception:  # noqa: BLE001 - untrusted result projection must fail closed
        return None


__all__ = [
    "OBSIDIAN_READ_TOOL_NAMES",
    "OBSIDIAN_TOOL_NAMES",
    "OBSIDIAN_WRITE_TOOL_NAMES",
    "ObsidianConversationIntent",
    "obsidian_conversation_intent",
    "obsidian_operation_id",
    "render_obsidian_tool_result",
]
