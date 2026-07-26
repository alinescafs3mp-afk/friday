"""Durable Telegram long-polling bridge with signed backend requests.

The bridge is assembled from one mixin per stage an update passes through: it
arrives (``_transport``), is routed by the command it names (``_commands``) or by
the button that was pressed (``_callbacks``), is answered (``_views``), and may
carry a file (``_media``). Each was lifted verbatim out of what used to be a
single 1670-line module.

``tests/test_bridge_surface.py`` pins the class surface and, more to the point,
both dispatch tables: a command branch or a button namespace that loses its
handler in a move fails silently — the bot just stops answering.
"""

from __future__ import annotations

# Re-exported so the split does not narrow what was importable from this package:
# every module-level name the single file used to expose still resolves here.
from jericho.telegram_bridge._base import (
    _SINGLE_MEDIA_FIELDS,
    API_BASE,
    BACKOFF_MAX,
    BATCH_SIZE,
    BOT_COMMANDS,
    CALLBACK_TARGET_RE,
    LOGGER,
    MAX_ATTEMPTS,
    POLL_TIMEOUT,
    RETRY_DELAYS_SEC,
    TELEGRAM_TEXT_LIMIT,
    MediaTooLargeError,
    PermanentUpdateError,
    TelegramConfig,
)
from jericho.telegram_bridge._callbacks import CallbacksMixin
from jericho.telegram_bridge._commands import CommandsMixin
from jericho.telegram_bridge._media import MediaMixin
from jericho.telegram_bridge._queue import _UpdateInbox
from jericho.telegram_bridge._transport import TransportMixin
from jericho.telegram_bridge._views import ViewsMixin

__all__ = [
    "API_BASE",
    "BACKOFF_MAX",
    "BATCH_SIZE",
    "BOT_COMMANDS",
    "CALLBACK_TARGET_RE",
    "LOGGER",
    "MAX_ATTEMPTS",
    "POLL_TIMEOUT",
    "RETRY_DELAYS_SEC",
    "TELEGRAM_TEXT_LIMIT",
    "MediaTooLargeError",
    "PermanentUpdateError",
    "TelegramBridge",
    "TelegramConfig",
    "_SINGLE_MEDIA_FIELDS",
    "_UpdateInbox",
]


class TelegramBridge(CallbacksMixin, CommandsMixin, MediaMixin, TransportMixin, ViewsMixin):
    """Durable Telegram long-polling bridge with signed backend requests."""
