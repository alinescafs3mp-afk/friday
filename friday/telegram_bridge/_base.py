"""Module-level foundations shared by the bridge mixins.

Constants, the command menu, the config record and the two update errors live here so
a mixin can use them without importing ``friday.telegram_bridge`` — which imports the
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
from urllib.parse import quote, urlsplit

import httpx

from friday.diagnostics.runtime_lease import ProcessLease, RuntimeLeaseError
from friday.security import sign_bridge_request
from friday.telemetry.logging import install_secret_redaction

# Named for the package, not this module: `__name__` here is "friday.telegram_bridge._base", and
# the split must not rename the logger operators already read in the logs.
LOGGER = logging.getLogger("friday.telegram_bridge")
API_BASE = "https://api.telegram.org"
# `getFile` on api.telegram.org refuses to serve bots files above 20 MB — a
# server-side ceiling no local setting can raise. `max_document_bytes` (wired
# from the backend's upload limit, 50 MB) still applies where it is stricter;
# downloads take the min of the two.
BOT_API_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024
POLL_TIMEOUT = 30
#: Виды обновлений, на которые подписан мост. Список ОДИН, потому что подписка и
#: разбор жили в разных местах и разошлись: опрос просил три вида, а чат обновления
#: искался только в `message`, и нажатая кнопка теряла адресата — человек нажимал
#: «Подтвердить», попытки исчерпывались, и он не узнавал об этом ничего.
ALLOWED_UPDATE_KINDS = ("message", "edited_message", "callback_query")
BACKOFF_MAX = 60.0
MAX_ATTEMPTS = 288
BATCH_SIZE = 20
#: Сколько обновлений мост обрабатывает ОДНОВРЕМЕННО.
#:
#: Число не с потолка. У ядра четыре передних слота для модели, и восемь дают
#: запас на ходы, которые до модели не доходят вовсе — команды, нажатия кнопок,
#: загрузка файла. Больше не нужно: очередь слотов на стороне ядра никуда не
#: делась, и лишние одновременные обращения просто ждали бы в ней, занимая
#: соединения. Меньше — и возвращается ровно та болезнь, ради которой это
#: заведено: один долгий ход держит чужие чаты.
MAX_CONCURRENT_UPDATES = 8
#: Сколько приглашений «ответьте новым текстом» помнить одновременно.
#: Человек либо отвечает сразу, либо передумал; десяти хватает с запасом.
_EDIT_TARGET_MEMORY = 10
# Telegram counts sendMessage's 4096 in UTF-16 code units, not code points.
TELEGRAM_TEXT_LIMIT = 4096
RETRY_DELAYS_SEC = (2.0, 10.0, 30.0, 60.0, 300.0)
#: Сколько мост помнит, что уведомление уже ушло человеку.
#:
#: В обычной жизни строка живёт секунды: отправили — записали — подтвердили
#: бэкенду — сняли. Неделю она может прожить только после падения ровно между
#: подтверждением и снятием; бэкенд такой номер больше не предложит никогда, и
#: без срока годности строка осталась бы в очереди навсегда.
DELIVERED_NOTIFICATION_TTL_SEC = 7 * 24 * 3600.0
CALLBACK_TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")
BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("chat", "обычный разговор"),
    ("work", "работа с личными знаниями"),
    ("research", "многошаговое исследование"),
    ("search", "поиск по базе без ответа модели"),
    ("history", "поиск по истории переписки"),
    ("source", "дословный поиск по исходным файлам"),
    ("timeline", "хроника периода: события и документы по их датам"),
    ("browse", "записи по тегу, проекту или сущности"),
    ("tags", "теги базы с количеством записей"),
    ("profile", "карточка сущности: документы, теги, даты, связи"),
    ("graph", "как связаны двое: цепочка связей между объектами"),
    ("entity_rename", "переименовать объект: старое имя => новое имя"),
    ("entity_alias", "добавить объекту псевдоним: объект => псевдоним"),
    ("watch", "следить за темой: сообщу, когда появится новое"),
    ("watching", "за чем слежу, и снять слежение"),
    ("approvals", "что ждёт вашего решения перед выполнением"),
    ("inbox", "разобрать ближайшие предложения"),
    ("conflicts", "разобрать конфликты знаний"),
    ("relations", "разобрать предложенные связи"),
    ("merges", "подтвердить объединение дубликатов"),
    ("mission", "многошаговая миссия в фоне"),
    ("missions", "список миссий и управление"),
    ("status", "состояние базы"),
    ("compact", "ночные сводки: что происходило с системой"),
    ("why", "почему был такой ответ: запрос, источники, что отброшено"),
    ("new", "начать новый диалог"),
    ("archive", "архивировать текущий разговор"),
    ("delete", "удалить текущий разговор"),
    ("rename", "переименовать текущий разговор"),
    ("note", "явно сохранить заметку"),
    ("instructions", "как отвечать: показать, задать или очистить"),
    ("retry", "сгенерировать ответ на последний вопрос заново"),
    ("reminders", "предстоящие напоминания и снять одно"),
    ("export", "скачать текущий разговор текстом"),
    ("help", "справка по командам"),
)


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    # Absolute, and deliberately without a default. This path locates BOTH the
    # durable update queue and the singleton lease beside it, so a cwd-relative
    # value is not a cosmetic wart: a bridge started from another directory opens
    # a different (empty) queue behind a different lock, which orphans undelivered
    # updates and lets a second bridge long-poll the same bot at the same time.
    inbox_db_path: str
    backend_url: str = "http://127.0.0.1:8000"
    # Public CA/certificate used only to authenticate the private Friday backend.
    # Telegram itself keeps httpx's independent standard public certifi roots.
    backend_ca_file: str = ""
    bridge_secret: str = ""
    allowed_chat_ids: list[int] = field(default_factory=list)
    max_document_bytes: int = 50 * 1024 * 1024
    backend_timeout_sec: float = 300.0
    outbound_poll_interval_sec: float = 15.0
    # Proxy for api.telegram.org only; the backend is always reached directly.
    telegram_proxy: str = ""
    # Mirrors settings.telegram_open_registration — see there for what it grants.
    open_registration: bool = False

    def validate(self) -> None:
        if not self.bot_token or ":" not in self.bot_token:
            raise ValueError("A valid Telegram bot token is required")
        if len(self.bridge_secret) < 32:
            raise ValueError("Telegram bridge secret must contain at least 32 characters")
        backend_scheme = ""
        try:
            parsed_backend = urlsplit(self.backend_url)
            backend_scheme = parsed_backend.scheme
            backend_hostname = parsed_backend.hostname or ""
            _ = parsed_backend.port
            valid_backend_url = bool(
                parsed_backend.scheme in {"http", "https"}
                and backend_hostname
                and not any(character.isspace() or ord(character) < 32 for character in backend_hostname)
                and not parsed_backend.username
                and not parsed_backend.password
                and parsed_backend.path in {"", "/"}
                and not parsed_backend.query
                and not parsed_backend.fragment
            )
        except ValueError:
            valid_backend_url = False
        if not valid_backend_url:
            raise ValueError(
                "backend_url must be an absolute HTTP(S) origin without credentials, path, query, or fragment"
            )
        if self.backend_ca_file and backend_scheme != "https":
            raise ValueError("backend_ca_file requires an HTTPS backend_url")
        if self.backend_ca_file and not Path(self.backend_ca_file).is_file():
            raise ValueError(f"backend_ca_file points to a missing file: {self.backend_ca_file}")
        if not self.inbox_db_path or not Path(self.inbox_db_path).is_absolute():
            # Fail closed before `_UpdateInbox` gets a chance to create the file:
            # a queue whose location depends on the cwd is not durable.
            raise ValueError("inbox_db_path must be an absolute path")
        if self.telegram_proxy and not self.telegram_proxy.startswith(("http://", "https://")):
            # SOCKS would need httpx's optional `socksio` extra, which Friday does
            # not depend on; a mixed proxy accepts HTTP CONNECT on the same port.
            raise ValueError("telegram_proxy must be an http:// or https:// URL")
        if not self.allowed_chat_ids:
            # Deny-by-default: refuse to run an open bot. The effective allowlist
            # (allowlist plus owner chats) is supplied by the caller.
            raise ValueError(
                "No allowed Telegram chats configured; set "
                "FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS or FRIDAY_TELEGRAM_OWNER_CHAT_IDS"
            )


def utf16_length(text: str) -> int:
    """Length as Telegram counts it: UTF-16 code units, so a non-BMP char costs 2."""
    return len(text) + sum(1 for character in text if ord(character) > 0xFFFF)


def _utf16_prefix_index(text: str, limit: int) -> int:
    """Largest ``i`` with ``utf16_length(text[:i]) <= limit``."""
    used = 0
    for index, character in enumerate(text):
        cost = 2 if ord(character) > 0xFFFF else 1
        if used + cost > limit:
            return index
        used += cost
    return len(text)


#: Парные знаки разметки, которые нельзя разрывать границей куска.
#:
#: Тройной обратный апостроф стоит первым намеренно: блок кода содержит одиночные,
#: и считать их до него значило бы вечно видеть непарный.
_MARKUP_PAIRS = ("```", "**", "~~", "`")


def _markup_is_balanced(piece: str) -> bool:
    """Все ли парные знаки разметки закрыты внутри куска."""
    rest = piece
    for token in _MARKUP_PAIRS:
        count = rest.count(token)
        if count % 2:
            return False
        rest = rest.replace(token, "")
    return True


def _split_without_breaking_markup(clean: str, split_at: int, hard: int) -> int:
    """Отодвинуть границу назад, пока пары разметки не окажутся целыми.

    Ищется ближайший перевод строки левее, потому что абзац — естественная
    граница и почти всегда стоит вне разметки.

    `floor` ограничивает ПОИСК, а не результат, и это важно понимать точно:
    когда целой границы не нашлось, возвращается ИСХОДНАЯ — текст не дробится и
    не теряется, просто разметка в этом месте не спасена. Доставка важнее
    начертания, и сломанную разметку `_send_message` всё равно переживает
    отправкой простым текстом. Проверено мутацией: снятие `floor` не меняет
    выдачу вовсе, только длину обратного просмотра, поэтому в обязательный список
    мутаций он не входит.
    """
    if _markup_is_balanced(clean[:split_at]):
        return split_at
    floor = max(1, hard // 4)
    candidate = split_at
    while candidate > floor:
        candidate = clean.rfind("\n", 0, candidate)
        if candidate <= floor:
            break
        if _markup_is_balanced(clean[:candidate]):
            return candidate
    return split_at


def split_for_telegram(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split a reply into sendMessage-sized chunks, preferring line then word breaks.

    Measured in UTF-16 code units because that is the unit Telegram's 4096 is in.
    ``len()`` counts code points, so a reply of 4092 code points containing five
    emoji is 4097 units and the API rejects the whole message with 400 — the user
    simply never receives the answer, and every emoji in it makes that more likely.
    """
    clean = str(text or "").strip() or "Готово."
    limit = max(1, int(limit))
    chunks: list[str] = []
    while clean:
        hard = _utf16_prefix_index(clean, limit)
        if hard >= len(clean):
            chunks.append(clean)
            break
        split_at = clean.rfind("\n", 0, hard)
        if split_at < hard // 2:
            split_at = clean.rfind(" ", 0, hard)
        if split_at < hard // 2:
            # No usable break: cut at the hard boundary. `hard` never lands inside a
            # surrogate pair, because the cost of a non-BMP character is charged
            # whole or not at all.
            split_at = hard
        # Граница не должна разрывать пару разметки. Раскол идёт по СЫРОМУ тексту,
        # а размечается каждый кусок отдельно: `**срок**`, разрезанное пополам,
        # оставляло человеку сырые звёздочки в обоих кусках. Отступаем к
        # ближайшему месту, где пары целы; если такого нет — режем как резали,
        # потому что доставка важнее начертания.
        split_at = _split_without_breaking_markup(clean, split_at, hard)
        piece = clean[:split_at].rstrip()
        if piece:
            chunks.append(piece)
        clean = clean[split_at:].lstrip()
    return chunks or ["Готово."]


def _redact_userinfo(url: str) -> str:
    """Strip ``user:password@`` from a URL so it is safe to log."""
    scheme, separator, rest = url.partition("://")
    if not separator or "@" not in rest:
        return url
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}"


def _proxy_password(url: str) -> str:
    """The password inside a proxy URL, if any, for the log redactor."""
    _, separator, rest = url.partition("://")
    if not separator or "@" not in rest:
        return ""
    userinfo, _, _ = rest.rpartition("@")
    _, has_password, password = userinfo.partition(":")
    return password if has_password else ""


class PermanentUpdateError(RuntimeError):
    """The update cannot become valid after a retry.

    Несёт `status_code`, потому что «нельзя повторять» — это ещё не «нечего
    показывать». 403 и 404 приходили сюда неразличимыми, и единственный обработчик
    печатал «ничего не нашлось»: человеку без права `kg.read` бот утверждал, что
    такого объекта в архиве НЕТ, — заявление о содержимом вместо отказа в доступе.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MediaTooLargeError(PermanentUpdateError):
    """A media file exceeded the configured size limit; the user is told, not dead-lettered silently."""


def refusal_notice(error: PermanentUpdateError) -> str:
    """Текст для отказа, который НЕЛЬЗЯ печатать как «ничего не нашлось».

    Пустая строка означает «это действительно про отсутствие» (404 или неизвестный
    статус) — тогда вызывающий печатает своё обычное сообщение о ненайденном.

    Разделение нужно потому, что «нет права смотреть» и «в архиве такого нет» —
    утверждения о РАЗНЫХ вещах, а человек в чате принимает второе за факт о своём
    архиве. У пользователя с кастомным пресетом без `kg.read` (ровно так устроен
    «newcomer») `/profile Иванов` отвечал «По имени «Иванов» ничего не нашлось» и
    советовал /browse — маршрут, который откажет так же.
    """
    status = getattr(error, "status_code", None)
    if status == 403:
        return "Смотреть объекты вам сейчас не разрешено — попросите доступ у владельца."
    if status in {400, 422}:
        return "Не понял запрос: проверьте написание имени."
    if status == 413:
        return "Запрос слишком большой — сократите имя."
    return ""


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
    _backend_text: Callable[..., Any]
    _extract_forward: Callable[..., Any]
    _file_url: Any
    _format_full_document: Callable[..., Any]
    _document_more_markup: Callable[..., Any]
    _reply_quote: Callable[..., Any]
    _album_caption: Callable[..., Any]
    _album_captions: dict[str, str]
    _send_message_returning_id: Callable[..., Any]
    _offer_access_to_owner: Callable[..., Any]
    _signer_chat_id: Callable[..., Any]
    _format_browse_results: Callable[..., Any]
    _format_status: Callable[..., Any]
    _format_timeline: Callable[..., Any]
    _timeline_reply_markup: Callable[..., Any]
    _format_mission_created: Callable[..., Any]
    _format_response_message: Callable[..., Any]
    _ENTITY_TYPE_CHOICES: Any
    _entity_type_markup: Callable[..., Any]
    _inbox: Any
    _may_message_chat: Callable[..., Any]
    _prepare_document: Callable[..., Any]
    _process_callback_query: Callable[..., Any]
    _process_update: Callable[..., Any]
    _response_reply_markup: Callable[..., Any]
    _send_browse: Callable[..., Any]
    _send_entity_profile: Callable[..., Any]
    _send_relation_path: Callable[..., Any]
    _send_inbox: Callable[..., Any]
    _send_conflicts: Callable[..., Any]
    _send_relations: Callable[..., Any]
    _send_history: Callable[..., Any]
    _send_merges: Callable[..., Any]
    _deliver_generated_files: Callable[..., Any]
    _send_reminders: Callable[..., Any]
    _send_message: Callable[..., Any]
    _send_document: Callable[..., Any]
    _send_voice: Callable[..., Any]
    _deliver_voice_reply: Callable[..., Any]
    _send_missions: Callable[..., Any]
    _send_search: Callable[..., Any]
    _send_tags: Callable[..., Any]
    _send_compacts: Callable[..., Any]
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
    "MAX_CONCURRENT_UPDATES",
    "_EDIT_TARGET_MEMORY",
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
    "_proxy_password",
    "_redact_userinfo",
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
    "refusal_notice",
    "sign_bridge_request",
    "sqlite3",
    "time",
    "uuid",
]
