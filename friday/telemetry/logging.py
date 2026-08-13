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
# Friday API credentials are minted as ``jrc_`` followed by 43 URL-safe
# characters.  Logs often contain a foreign/user token which is not present in
# this process environment, so exact-value filtering alone cannot protect it.
# Keep the lower bound high enough that ordinary labels such as ``jrc_demo``
# are not mistaken for credentials.
_FRIDAY_API_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])jrc_[A-Za-z0-9_-]{40,}")
# A Telegram bot token lives in the URL PATH — `api.telegram.org/bot<id>:<secret>/…`
# — so it is not an assignment, not userinfo and not a `sk-` key: none of the
# patterns above see it. Every httpx error quotes the failing URL, which is how
# it reached the dead-letter queue and from there `jericho doctor`. Matched on
# the shape (`/bot` + digits + colon + a long opaque tail) so it is removed even
# when the process doing the logging has no idea what the token is.
_TELEGRAM_BOT_TOKEN = re.compile(r"(?i)(/bot)(\d{5,}:[A-Za-z0-9_-]{20,})")

# Route families are source-owned labels.  Merely accepting a syntactically neat
# segment is not enough: a client controls 404 paths and could otherwise make an
# arbitrary word appear in the durable access log.
_ACCESS_API_FAMILIES = frozenset(
    {
        "admin",
        "approvals",
        "assistant",
        "chat",
        "chronicle",
        "compacts",
        "conversations",
        "docs",
        "events",
        "feedback",
        "files",
        "health",
        "import",
        "inbox",
        "ingest",
        "kg",
        "knowledge",
        "me",
        "missions",
        "notifications",
        "openapi.json",
        "profile",
        "reflection",
        "research",
        "search",
    }
)
_ACCESS_ROOT_FAMILIES = frozenset({"admin", "api", "health"})
_ACCESS_METHODS = frozenset({"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"})
_SAFE_EXCEPTION_KINDS = frozenset(
    {
        "AssertionError",
        "ConnectionError",
        "Exception",
        "LookupError",
        "OSError",
        "RuntimeError",
        "TimeoutError",
        "TypeError",
        "ValueError",
    }
)


def redact_friday_api_tokens(value: Any) -> str:
    """Remove structurally valid Friday API tokens from an outward string."""

    return _FRIDAY_API_TOKEN.sub("[redacted:token]", str(value))


def redact_text(value: Any) -> str:
    """Redact common credential forms without trying to infer arbitrary data."""

    text = str(value)
    text = _URL_USERINFO.sub(r"\1[redacted]@", text)
    text = _AUTH_VALUE.sub(r"\1[redacted]", text)
    text = _PRIVATE_KEY.sub("[redacted:private-key]", text)
    text = _KNOWN_TOKEN.sub("[redacted:token]", text)
    text = redact_friday_api_tokens(text)
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
    """Project Uvicorn access records to method, route family and status.

    Search queries and browse filters travel as URL parameters
    (``/api/search?q=…``), while path segments carry entity, conversation,
    file and account identifiers.  Client addresses are identifiers too.  The
    secret redactor cannot recognise any of those, so keep only the first two
    API route segments (one elsewhere), query presence, method and status.
    """

    @staticmethod
    def _project_path(value: object) -> str:
        full_path = str(value)
        path, separator, _query = full_path.partition("?")
        segments = [segment for segment in path.split("/") if segment]
        if segments[:1] == ["api"]:
            family = segments[1] if len(segments) > 1 else None
            safe = family if family in _ACCESS_API_FAMILIES else "<unknown>"
            path = f"/api/{safe}"
            if len(segments) > 2:
                path += "/[...]"
        elif segments:
            family = segments[0]
            safe = family if family in _ACCESS_ROOT_FAMILIES else "<unknown>"
            path = f"/{safe}"
            if len(segments) > 1:
                path += "/[...]"
        else:
            path = "/"
        return f"{path}?[stripped]" if separator else path

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn.access args: (client_addr, method, full_path, http_version, status)
        if isinstance(args, tuple) and len(args) == 5:
            proposed_method = str(args[1]).upper()
            method = proposed_method if proposed_method in _ACCESS_METHODS else "<unknown-method>"
            record.args = ("<client>", method, self._project_path(args[2]), *args[3:])
        return True


class ExternalExceptionStripper(logging.Filter):
    """Drop exception messages and tracebacks emitted by framework loggers.

    Uvicorn logs an unhandled ASGI exception with ``exc_info``.  HTTP/client and
    parser exceptions may embed request URLs, submitted text or local paths, none
    of which a credential regex can recognise.  Keep only the exception class as
    operational signal and replace the framework message with a fixed event.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info or record.stack_info:
            kind = "Exception"
            exc_info = record.exc_info
            if isinstance(exc_info, tuple) and exc_info and isinstance(exc_info[0], type):
                kind = exc_info[0].__name__
            elif isinstance(exc_info, BaseException):
                kind = type(exc_info).__name__
            if kind not in _SAFE_EXCEPTION_KINDS:
                kind = "Exception"
            record.msg = "ASGI application failed (%s)"
            record.args = (kind,)
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def install_access_log_privacy() -> None:
    """Attach the query-string stripper to uvicorn's access logger."""

    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, AccessLogQueryStripper) for item in logger.filters):
        logger.addFilter(AccessLogQueryStripper())


def install_external_exception_privacy() -> None:
    """Prevent Uvicorn from persisting arbitrary exception text or traceback."""

    logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(item, ExternalExceptionStripper) for item in logger.filters):
        logger.addFilter(ExternalExceptionStripper())


def configure_secure_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    install_secret_redaction(secrets)
    install_external_exception_privacy()
    logging.getLogger().setLevel(getattr(logging, str(level).upper(), logging.INFO))


__all__ = [
    "AccessLogQueryStripper",
    "ExternalExceptionStripper",
    "SecretRedactingFormatter",
    "configure_secure_logging",
    "install_access_log_privacy",
    "install_external_exception_privacy",
    "install_secret_redaction",
    "redact_text",
    "secrets_from_environment",
]
