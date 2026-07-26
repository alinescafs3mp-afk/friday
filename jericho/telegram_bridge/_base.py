"""Module-level foundations shared by the bridge mixins.

Constants, the command menu, the config record and the two update errors live here so
a mixin can use them without importing ``jericho.telegram_bridge`` — which imports the
mixins, and would be a cycle. The package re-exports the public names.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from jericho.diagnostics.runtime_lease import ProcessLease, RuntimeLeaseError
from jericho.security import sign_bridge_request
from jericho.telemetry.logging import install_secret_redaction

LOGGER = logging.getLogger(__name__)
API_BASE = "https://api.telegram.org"
POLL_TIMEOUT = 30
BACKOFF_MAX = 60.0
MAX_ATTEMPTS = 288
BATCH_SIZE = 20
TELEGRAM_TEXT_LIMIT = 4096
RETRY_DELAYS_SEC = (2.0, 10.0, 30.0, 60.0, 300.0)
CALLBACK_TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")
BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("chat", "обычный разговор"),
    ("work", "работа с личными знаниями"),
    ("research", "многошаговое исследование"),
    ("search", "поиск по базе без ответа модели"),
    ("browse", "записи по тегу, проекту или сущности"),
    ("tags", "теги базы с количеством записей"),
    ("inbox", "разобрать ближайшие предложения"),
    ("merges", "подтвердить объединение дубликатов"),
    ("mission", "многошаговая миссия в фоне"),
    ("missions", "список миссий и управление"),
    ("status", "состояние базы"),
    ("new", "начать новый диалог"),
    ("note", "явно сохранить заметку"),
    ("help", "справка по командам"),
)


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    backend_url: str = "http://127.0.0.1:8000"
    bridge_secret: str = ""
    allowed_chat_ids: list[int] = field(default_factory=list)
    inbox_db_path: str = "telegram_inbox.sqlite3"
    max_document_bytes: int = 50 * 1024 * 1024
    backend_timeout_sec: float = 300.0
    outbound_poll_interval_sec: float = 15.0

    def validate(self) -> None:
        if not self.bot_token or ":" not in self.bot_token:
            raise ValueError("A valid Telegram bot token is required")
        if len(self.bridge_secret) < 32:
            raise ValueError("Telegram bridge secret must contain at least 32 characters")
        if not self.backend_url.startswith(("http://", "https://")):
            raise ValueError("backend_url must use HTTP or HTTPS")
        if not self.allowed_chat_ids:
            # Deny-by-default: refuse to run an open bot. The effective allowlist
            # (allowlist plus owner chats) is supplied by the caller.
            raise ValueError(
                "No allowed Telegram chats configured; set "
                "JERICHO_TELEGRAM_ALLOWED_CHAT_IDS or JERICHO_TELEGRAM_OWNER_CHAT_IDS"
            )


class PermanentUpdateError(RuntimeError):
    """The update cannot become valid after a retry."""


class MediaTooLargeError(PermanentUpdateError):
    """A media file exceeded the configured size limit; the user is told, not dead-lettered silently."""


_SINGLE_MEDIA_FIELDS: tuple[tuple[str, str, str, str, bool], ...] = (
    # (message field, media_kind, default mime, filename suffix, use file_name)
    ("document", "document", "application/octet-stream", "bin", True),
    ("voice", "voice", "audio/ogg", "ogg", False),
    ("audio", "audio", "audio/mpeg", "mp3", True),
    ("video", "video", "video/mp4", "mp4", True),
    ("video_note", "video_note", "video/mp4", "mp4", False),
    ("animation", "animation", "video/mp4", "mp4", True),
)


class BridgeShared:
    """What a bridge mixin may rely on its siblings providing.

    The bridge is assembled from mixins, so within one module the transport, the two
    dispatch tables and the views call each other through a class the type checker
    cannot see. Declared as annotations — nothing is defined here, so no method is
    shadowed — which keeps the checking honest instead of silencing it. What actually
    guarantees these exist is ``tests/test_bridge_surface.py``.

    Only members that genuinely cross a module boundary belong here.
    """

    _api_url: Any
    _backend_json: Callable[..., Any]
    _extract_forward: Callable[..., Any]
    _file_url: Any
    _format_mission_created: Callable[..., Any]
    _format_response_message: Callable[..., Any]
    _inbox: Any
    _prepare_document: Callable[..., Any]
    _process_callback_query: Callable[..., Any]
    _process_update: Callable[..., Any]
    _response_reply_markup: Callable[..., Any]
    _send_browse: Callable[..., Any]
    _send_inbox: Callable[..., Any]
    _send_merges: Callable[..., Any]
    _send_message: Callable[..., Any]
    _send_missions: Callable[..., Any]
    _send_search: Callable[..., Any]
    _send_tags: Callable[..., Any]
    _structured_text: Callable[..., Any]
    _typing_loop: Callable[..., Any]
    _unsupported_label: Callable[..., Any]
    config: Any


# Imported by the mixins; several are unused inside this file, and `ruff --fix`
# would strip them as dead without the explicit list.
__all__ = [
    "API_BASE",
    "Any",
    "BACKOFF_MAX",
    "BATCH_SIZE",
    "BOT_COMMANDS",
    "BridgeShared",
    "CALLBACK_TARGET_RE",
    "LOGGER",
    "MAX_ATTEMPTS",
    "MediaTooLargeError",
    "POLL_TIMEOUT",
    "Path",
    "PermanentUpdateError",
    "ProcessLease",
    "RETRY_DELAYS_SEC",
    "RuntimeLeaseError",
    "TELEGRAM_TEXT_LIMIT",
    "TelegramConfig",
    "_SINGLE_MEDIA_FIELDS",
    "asyncio",
    "base64",
    "dataclass",
    "field",
    "httpx",
    "install_secret_redaction",
    "json",
    "logging",
    "quote",
    "re",
    "sign_bridge_request",
    "sqlite3",
    "time",
    "uuid",
]
