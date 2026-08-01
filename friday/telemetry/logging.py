"""Secret-aware local logging helpers.

The Telegram Bot API embeds the bot credential in request URLs, so redaction is
performed after the complete log record (including traceback text) is rendered.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_REDACTED = "[REDACTED]"
_SECRET_ENV_KEY = re.compile(
    r"(?i)(?:^|_)(?:password|passwd|secret|token|credential|authorization|cookie|"
    r"private_key|api_key)$"
)
_AUTH_VALUE = re.compile(r"(?i)\b((?:Bearer|Basic)\s+)[A-Za-z0-9._~+/=-]{4,}")
_ASSIGNMENT = re.compile(
    r"(?i)([\"']?[A-Za-z0-9_.-]*(?:password|passwd|secret|token|credential|"
    r"private[_-]?key|api[_-]?key)[A-Za-z0-9_.-]*[\"']?)(\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;\]\}]+)"
)
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\s]+)@")
_PRIVATE_KEY = re.compile(r"(?is)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----")
_KNOWN_TOKEN = re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{12,})")
# A Telegram bot token lives in the URL PATH — `api.telegram.org/bot<id>:<secret>/…`
# — so it is not an assignment, not userinfo and not a `sk-` key: none of the
# patterns above see it. Every httpx error quotes the failing URL, which is how
# it reached the dead-letter queue and from there `jericho doctor`. Matched on
# the shape (`/bot` + digits + colon + a long opaque tail) so it is removed even
# when the process doing the logging has no idea what the token is.
_TELEGRAM_BOT_TOKEN = re.compile(r"(?i)(/bot)(\d{5,}:[A-Za-z0-9_-]{20,})")


def redact_text(value: Any) -> str:
    """Redact common credential forms without trying to infer arbitrary data."""

    text = str(value)
    text = _URL_USERINFO.sub(r"\1[redacted]@", text)
    text = _AUTH_VALUE.sub(r"\1[redacted]", text)
    text = _PRIVATE_KEY.sub("[redacted:private-key]", text)
    text = _KNOWN_TOKEN.sub("[redacted:token]", text)
    text = _TELEGRAM_BOT_TOKEN.sub(r"\1[redacted:token]", text)
    return _ASSIGNMENT.sub(r"\1\2[redacted]", text)


def secrets_from_environment() -> tuple[str, ...]:
    """Return explicitly secret environment values for exact-value redaction."""

    values: set[str] = set()
    for key, value in os.environ.items():
        if len(value) >= 4 and _SECRET_ENV_KEY.search(key):
            values.add(value)
    return tuple(sorted(values, key=len, reverse=True))


class SecretRedactingFormatter(logging.Formatter):
    """Render a record and then remove exact and structurally obvious secrets."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__(LOG_FORMAT, DATE_FORMAT)
        variants: set[str] = set()
        for raw_secret in secrets:
            secret = str(raw_secret or "")
            if not secret:
                continue
            encoded = quote(secret, safe="")
            variants.update({secret, encoded, encoded.replace("%3A", "%3a")})
        self._secrets = tuple(sorted(variants, key=len, reverse=True))

    def _redact(self, text: str) -> str:
        rendered = redact_text(text)
        for secret in self._secrets:
            rendered = rendered.replace(secret, _REDACTED)
        return rendered

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        if record.exc_text:
            record.exc_text = self._redact(record.exc_text)
        return self._redact(rendered)


def install_secret_redaction(secrets: Iterable[str] = ()) -> None:
    """Install redacting formatters on every root handler."""

    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    formatter = SecretRedactingFormatter(secrets)
    for handler in root.handlers:
        handler.setFormatter(formatter)
    # Access-style INFO logs contain complete request URLs.  Keep them quiet in
    # addition to formatter-level redaction as defense in depth.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class AccessLogQueryStripper(logging.Filter):
    """Strip query strings from uvicorn access-log records.

    Search queries and browse filters travel as URL parameters
    (``/api/search?q=…``), so the raw request line is personal data. The
    secret redactor cannot recognise free-form queries — the whole query
    string is dropped instead, keeping method/path/status observability.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn.access args: (client_addr, method, full_path, http_version, status)
        if isinstance(args, tuple) and len(args) == 5:
            path = str(args[2])
            if "?" in path:
                record.args = (*args[:2], f"{path.split('?', 1)[0]}?[stripped]", *args[3:])
        return True


def install_access_log_privacy() -> None:
    """Attach the query-string stripper to uvicorn's access logger."""

    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, AccessLogQueryStripper) for item in logger.filters):
        logger.addFilter(AccessLogQueryStripper())


def configure_secure_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    install_secret_redaction(secrets)
    logging.getLogger().setLevel(getattr(logging, str(level).upper(), logging.INFO))


__all__ = [
    "AccessLogQueryStripper",
    "SecretRedactingFormatter",
    "configure_secure_logging",
    "install_access_log_privacy",
    "install_secret_redaction",
    "redact_text",
    "secrets_from_environment",
]
