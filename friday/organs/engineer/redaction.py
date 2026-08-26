"""Deterministic secret stripping for engineer evidence.

Engineer inputs are adversarial data.  Reports may name the *kind* of secret
that was observed, but raw credentials, cookies and URL query values must not
become model context, durable evidence, or logs.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_SPACE = re.compile(r"\s+")
_APPLICATION_MARKUP = re.compile(
    r"(?:</?\s*(?:tool_call|tool_result|function_call|assistant|system|developer|user)"
    r"(?:\s[^>]*)?>|"
    r"<\|(?:im_start|im_end|system|assistant|developer|user|tool|tool_call|tool_result)\|>|"
    r"\[/?INST\]|<<\s*/?SYS\s*>>)",
    re.IGNORECASE,
)
_PEM = re.compile(
    r"-----BEGIN [^-\r\n]{0,80}(?:PRIVATE KEY|CREDENTIALS?)[^-\r\n]*-----"
    r".*?(?:-----END [^-\r\n]{0,80}-----|$)",
    re.IGNORECASE | re.DOTALL,
)
_AUTH = re.compile(r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_URI_USERINFO = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]{0,31}://)[^/@\s]+@",
    re.IGNORECASE,
)
_URL_TOKEN = re.compile(
    r"\b[a-z][a-z0-9+.-]{0,31}://[^\s<>'\"]+",
    re.IGNORECASE,
)
_URL_SCHEME = re.compile(r"[a-z][a-z0-9+.-]{0,31}", re.IGNORECASE)
_ASSIGNMENT = re.compile(
    r"(?P<key>[\"']?\b(?:pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?key|"
    r"client[_-]?secret|session(?:id)?|auth(?:orization)?)\b[\"']?)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>[\"']?[^\s,;}{]{4,}[\"']?)",
    re.IGNORECASE,
)
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9_+/=-])[A-Za-z0-9_+/=-]{48,}(?![A-Za-z0-9_+/=-])")
_PREFIXED_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,})\b",
    re.IGNORECASE,
)


def _project_url(value: str) -> str:
    """Return a URL-shaped projection with no credentials or value-bearing query."""

    scheme, separator, _remainder = value.partition("://")

    def invalid_projection() -> str:
        if separator and _URL_SCHEME.fullmatch(scheme):
            return f"{scheme}://[REDACTED_INVALID_URL]"
        return value

    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if not parsed.scheme or not host:
            return invalid_projection()
        authority = f"[{host}]" if ":" in host and not host.startswith("[") else host
        if parsed.port is not None:
            authority = f"{authority}:{parsed.port}"
        query = urlencode(
            [
                (key, "[REDACTED]" if query_value else "")
                for key, query_value in parse_qsl(parsed.query, keep_blank_values=True)[:24]
            ]
        )
        return urlunsplit((parsed.scheme, authority, parsed.path[:160], query, ""))
    except (TypeError, ValueError):
        return invalid_projection()


def _redact_generic(text: str, *, single_line: bool) -> str:
    """Apply non-URL secret patterns without re-entering URL projection."""

    text = _PEM.sub("[REDACTED_PRIVATE_MATERIAL]", text)
    text = _URI_USERINFO.sub(lambda match: match.group("scheme"), text)
    text = _AUTH.sub("[REDACTED_AUTH]", text)
    text = _ASSIGNMENT.sub(lambda match: f"{match.group('key')}{match.group('sep')}[REDACTED]", text)
    text = _AWS_ACCESS_KEY.sub("[REDACTED_ACCESS_KEY]", text)
    text = _JWT.sub("[REDACTED_JWT]", text)
    text = _PREFIXED_TOKEN.sub("[REDACTED_TOKEN]", text)
    text = _LONG_TOKEN.sub("[REDACTED_TOKEN]", text)
    if single_line:
        text = _SPACE.sub(" ", text).strip()
    return text


def redact_text(value: object, *, limit: int = 512, single_line: bool = True) -> str:
    """Return a bounded, stable projection with common credential forms removed."""

    text = _ANSI.sub("", str(value or ""))
    text = _CONTROL.sub("", text)
    text = _APPLICATION_MARKUP.sub("[APPLICATION_MARKUP_REMOVED]", text)
    text = _URL_TOKEN.sub(lambda match: _project_url(match.group()), text)
    text = _redact_generic(text, single_line=single_line)
    return text[: max(0, int(limit))]


def redact_url(value: object, *, limit: int = 256) -> str:
    """Strip userinfo, fragments, and every non-empty query value from a URL."""

    raw = _CONTROL.sub("", str(value or "").strip())
    projected = _project_url(raw)
    return _redact_generic(projected, single_line=True)[: max(0, int(limit))]


def redact_header(name: object, value: object, *, limit: int = 180) -> str:
    """Project response headers without retaining cookie or auth material."""

    key = str(name or "").strip().casefold()
    raw = str(value or "")
    if key == "set-cookie":
        cookie_names: list[str] = []
        for segment in raw.split(",")[:12]:
            pair = segment.strip().split(";", 1)[0]
            name_part = pair.split("=", 1)[0].strip()
            if name_part and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name_part):
                cookie_names.append(name_part)
        return ", ".join(f"{item}=[REDACTED]" for item in dict.fromkeys(cookie_names))[:limit]
    if key in {"authorization", "proxy-authorization"}:
        return "[REDACTED_AUTH]"
    if key == "www-authenticate":
        scheme = raw.strip().split(None, 1)[0][:32]
        return f"{scheme} [REDACTED_CHALLENGE]" if scheme else "[REDACTED_CHALLENGE]"
    if key == "location":
        return redact_url(raw, limit=limit)
    return redact_text(raw, limit=limit)


__all__ = ["redact_header", "redact_text", "redact_url"]
