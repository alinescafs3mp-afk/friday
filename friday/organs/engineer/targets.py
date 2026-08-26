"""Code-owned target selection for the engineer workbench.

Only the current human speech may mint a target. Model output can refer to the
resulting :class:`PinnedTarget`, but cannot construct one from a free-form host
argument.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit

_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b")
_IPV6 = re.compile(r"(?<![0-9a-f:])(?:[0-9a-f]{1,4}:){2,7}[0-9a-f:.]{1,15}(?![0-9a-f:])", re.IGNORECASE)
_IPV6_LOOSE = re.compile(
    r"(?<![0-9a-z])(?:\[[0-9a-f:.]+\](?::\d{1,5})?|[0-9a-f:.]*:[0-9a-f:.]*:[0-9a-f:.]*)(?![0-9a-z])",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_HOSTNAME = re.compile(
    r"\b(?:localhost|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})\b",
    re.IGNORECASE,
)
_TRAILING = re.compile(r"[),.;,]+$")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)
_CIDR = re.compile(
    r"(?<![0-9a-f:.])(?:"
    r"(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)|"
    r"[0-9a-f:]*:[0-9a-f:.]+"
    r")/[^\s,;.!?()\[\]{}<>\"']*",
    re.IGNORECASE,
)
_ACTIVE_ASSESSMENT_VERB = re.compile(
    r"\A\s*(?:(?:hi|hello|hey|привет|здравствуй(?:те)?)[!,.;:\s]+)?"
    r"(?:(?:and|so|then|now|а|и|ну|так|тогда|теперь|сейчас|уже)\s+){0,2}"
    r"(?:(?:please|pls|kindly|can\s+you|could\s+you|would\s+you|"
    r"i\s+(?:want|need|ask|authorize)\s+you\s+to|"
    r"пожалуйста|прошу|можешь|можете|сможешь|сможете|"
    r"(?:не\s+)?мог(?:ла|ли)?\s+бы(?:\s+(?:ты|вы))?|давай(?:те)?|"
    r"нужно|надо|хочу|разрешаю)\s*[,;:]?\s+)?"
    r"(?:(?:now|then|finally|теперь|сейчас|уже|наконец)\s+)?"
    r"(?:actively\s+|активно\s+)?(?:"
    r"scan|probe|audit|assess|inspect|enumerate|discover|check|test|"
    r"run\s+(?:an?\s+)?(?:scan|probe|audit|assessment|inspection)|"
    r"start\s+(?:an?\s+)?(?:scan|probe|audit)|"
    r"perform\s+(?:an?\s+)?(?:scan|probe|audit|assessment|inspection)|"
    r"просканиру(?:й|йте|ем)|просканировать|сканиру(?:й|йте|ем)|сканировать|"
    r"проверь(?:те)?|провер(?:ь|ьте)|проверить|"
    r"проаудиру(?:й|йте)|проаудировать|обследу(?:й|йте)|обследовать|"
    r"исследу(?:й|йте)|исследовать|"
    r"запусти(?:те)?\s*(?:,\s*пожалуйста\s*,?\s*)?(?:сканирование|проверку|аудит)|"
    r"проведи(?:те)?\s*(?:,\s*пожалуйста\s*,?\s*)?"
    r"(?:сканирование|проверку|аудит|обследование)"
    r")\b",
    re.IGNORECASE,
)
_ACTIVE_ASSESSMENT_NEGATION = re.compile(
    r"(?:\b(?:do\s+not|don't|dont|never|without)\b[^.!?\n]{0,48}\b"
    r"(?:scan|probe|audit|assess|inspect|enumerate|discover|check|test)\w*\b|"
    r"\b(?:не|никогда|без)\b[^.!?\n]{0,48}\b"
    r"(?:скан|провер|аудит|обслед|исслед|развед)\w*\b)",
    re.IGNORECASE,
)
_PASSIVE_ASSESSMENT_OBJECT = re.compile(
    r"\b(?:report|results?|log|document|file|text|article|screenshot|"
    r"configuration|settings?|status|topology|diagram|inventory|connection|connectivity|"
    r"отч[её]т\w*|результат\w*|лог\w*|документ\w*|файл\w*|текст\w*|"
    r"стать\w*|скриншот\w*|конфигурац\w*|настройк\w*|"
    r"состоян\w*|статус\w*|тополог\w*|схем\w*|инвентар\w*|"
    r"подключен\w*|соединен\w*|соединён\w*)\b",
    re.IGNORECASE,
)
_CONFIGURED_NETWORK_OBJECT = re.compile(
    r"\b(?:"
    r"my\s+(?:(?:local|home|private)\s+)?(?:subnet|network|lan)|"
    r"(?:local|home|private)\s+(?:subnet|network|lan)|"
    r"мо(?:ю|я|ей)\s+(?:(?:локальн|домашн|частн)\w*\s+)?(?:подсет\w*|сет\w*)|"
    r"(?:локальн|домашн|частн)\w*\s+(?:подсет\w*|сет\w*)"
    r")\b",
    re.IGNORECASE,
)
_CONFIGURED_NETWORK_ACTIVE_VERB = re.compile(
    r"\A\s*(?:(?:hi|hello|hey|привет|здравствуй(?:те)?)[!,.;:\s]+)?"
    r"(?:(?:and|so|then|now|а|и|ну|так|тогда|теперь|сейчас|уже)\s+){0,2}"
    r"(?:(?:please|pls|kindly|can\s+you|could\s+you|would\s+you|"
    r"i\s+(?:want|need|ask|authorize)\s+you\s+to|"
    r"пожалуйста|прошу|можешь|можете|сможешь|сможете|"
    r"(?:не\s+)?мог(?:ла|ли)?\s+бы(?:\s+(?:ты|вы))?|давай(?:те)?|"
    r"нужно|надо|хочу|разрешаю)\s*[,;:]?\s+)?"
    r"(?:(?:now|then|finally|теперь|сейчас|уже|наконец)\s+)?"
    r"(?:actively\s+|активно\s+)?(?:"
    r"scan|probe|audit|enumerate|discover|"
    r"run\s+(?:an?\s+)?(?:scan|probe|audit)|"
    r"start\s+(?:an?\s+)?(?:scan|probe|audit)|"
    r"perform\s+(?:an?\s+)?(?:scan|probe|audit)|"
    r"просканиру(?:й|йте|ем)|просканировать|сканиру(?:й|йте|ем)|сканировать|"
    r"проаудиру(?:й|йте)|проаудировать|обследу(?:й|йте)|обследовать|"
    r"исследу(?:й|йте)|исследовать|"
    r"(?:use|run)\s+(?:nmap|a\s+(?:network|port)\s+scan)|"
    r"(?:используй|используйте|запусти|запустите)\s+(?:nmap|сканер\w*)|"
    r"запусти(?:те)?\s*(?:,\s*пожалуйста\s*,?\s*)?(?:сканирование|аудит)|"
    r"проведи(?:те)?\s*(?:,\s*пожалуйста\s*,?\s*)?"
    r"(?:сканирование|аудит|обследование)"
    r")\b",
    re.IGNORECASE,
)
_NETWORK_SCAN_MECHANISM = re.compile(
    r"\b(?:nmap|scanner|network\s+scan|port\s+scan|"
    r"сканер\w*|сканирован\w*|скан\w*\s+порт\w*)\b",
    re.IGNORECASE,
)
_REQUEST_CODE_TEXT = re.compile(
    r"```[\s\S]*?(?:```|\Z)|~~~[\s\S]*?(?:~~~|\Z)|`[^`\r\n]*(?:`|$)",
    re.MULTILINE,
)
_QUOTED_REQUEST_TEXT = re.compile(
    r"«[^»]*»|“[^”]*”|„[^“]*“|\"[^\"\r\n]*\"|'[^'\r\n]*'",
)
_UNTERMINATED_REQUEST_QUOTE = re.compile(r"(?m)[«“„\"][^\r\n]*$")
_REQUEST_BLOCKQUOTE = re.compile(r"(?m)^[ \t]*>[^\r\n]*$")
_REQUEST_UNIT_BOUNDARY = re.compile(r"(?:[!?;]+(?:\s+|$)|\.(?:\s+|$)|\n+)")
_REQUEST_SOFT_BOUNDARY = re.compile(r"(?:,\s+|\s+[—–-]\s+)")
_REPORTED_REQUEST_CUE = re.compile(
    r"\b(?:сказа\w*|говор\w*|написа\w*|указа\w*|попрос\w*|просил\w*|велел\w*|"
    r"предлож\w*|посовет\w*|требу\w*|цитир\w*|цитат\w*|повтор\w*|"
    r"перевед\w*|означа\w*|said|says?|wrote|told|asked|ordered|"
    r"suggested|recommended|required|quote\w*|repeat\w*|translat\w*|means?)\b",
    re.IGNORECASE,
)
_META_REQUEST_CUE = re.compile(
    r"\b(?:пример\w*|фраз\w*|цитат\w*|инструкц\w*|шаблон\w*|"
    r"команд\w*|examples?|phrases?|quot(?:e|es|ed)|instructions?|templates?|commands?)\b",
    re.IGNORECASE,
)
_CONDITIONAL_REQUEST_CUE = re.compile(r"\b(?:если|if|unless)\b", re.IGNORECASE)
_POLITE_NEGATIVE_MODAL = re.compile(
    r"\bне\s+мог(?:ла|ли)?\s+бы(?:\s+(?:ты|вы))?\b",
    re.IGNORECASE,
)
_TRAILING_REQUEST_CANCEL = re.compile(
    r"(?:\b(?:но|однако|хотя|but|however)\b[^.!?\n]{0,40})?"
    r"(?:\bне\s+(?:надо|нужно|стоит|делай(?:те)?|выполняй(?:те)?|"
    r"запускай(?:те)?|сканируй(?:те)?)\b|"
    r"\b(?:отмена|отмени(?:те)?|передумал(?:а)?)\b|"
    r"\b(?:do\s+not|don't|dont|never)\s+(?:do|scan|run|execute)\b|"
    r"\b(?:cancel(?:\s+(?:it|that))?|never\s+mind)\b)",
    re.IGNORECASE,
)
_TRAILING_REQUEST_ATTRIBUTION = re.compile(
    r"(?:[,;]\s*|\s+[—–-]\s+|(?:^|[.!?])\s*)"
    r"(?:это\s+(?:пример|цитата|фраза|команда)|"
    r"так\s+(?:сказал|написал)\w*|"
    r"this\s+is\s+(?:an?\s+)?(?:example|quote|phrase|command)|"
    r"(?:as\s+)?(?:said|written)\s+by)\b",
    re.IGNORECASE,
)
_EXPLICIT_REQUEST_CONTEXT_RESET = re.compile(
    r"\A\s*(?:(?:а|и|and|so)\s+)?(?:теперь|сейчас|наконец|now|then|finally)\b",
    re.IGNORECASE,
)
_ARTIFACT_PATCH_REQUEST = re.compile(
    r"\A\s*(?:(?:please|pls|kindly|can\s+you|could\s+you|would\s+you|"
    r"i\s+(?:want|need|ask|authorize)\s+you\s+to|"
    r"пожалуйста|прошу|можешь|можете|нужно|надо|хочу|разрешаю)\s*[,;:]?\s+)?"
    r"(?:patch|fix|modify|edit|rewrite|change|repair|"
    r"apply\s+(?:the\s+)?(?:patch|changes?)\s+to|"
    r"(?:produce|create|generate)\s+[^.!?\n]{0,64}\b(?:derived|patched|modified)|"
    r"исправь|исправьте|почини|почините|измени|измените|"
    r"отредактируй|отредактируйте|перепиши|перепишите|"
    r"пропатчь|пропатчьте|модифицируй|модифицируйте|"
    r"примени(?:те)?\s+(?:патч|изменения)\s+(?:к|для)|"
    r"(?:создай|создайте|сгенерируй|сгенерируйте)\s+[^.!?\n]{0,64}\b"
    r"(?:исправленн|измен[её]нн|пропатченн)\w*)\b"
    r"[^.!?\n]{0,120}\b(?:artifact|file|binary|executable|archive|attachment|"
    r"it|this|артефакт\w*|файл\w*|бинарн\w*|исполняем\w*|архив\w*|"
    r"вложени\w*|его|е[её]|это)\b",
    re.IGNORECASE,
)
_ARTIFACT_PATCH_NEGATION = re.compile(
    r"(?:\b(?:do\s+not|don't|dont|never|without)\b[^.!?\n,;]{0,48}\b"
    r"(?:patch|fix|modify|edit|rewrite|change|repair)\b|"
    r"\b(?:не|никогда|без)\b[^.!?\n,;]{0,48}\b"
    r"(?:исправ|почин|измен|редактир|перепис|патч|модифицир)\w*\b)",
    re.IGNORECASE,
)
_METADATA_V4 = ipaddress.ip_address("169.254.169.254")
_METADATA_V6 = ipaddress.ip_address("fd00:ec2::254")
_OTHER_METADATA_V4 = frozenset(
    {
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("192.0.0.192"),
    }
)


@dataclass(frozen=True, slots=True)
class PinnedTarget:
    """Resolved, current-speech authority consumed by network stages."""

    host: str
    addresses: tuple[str, ...]
    implied_port: int | None
    source_token: str
    source_sha256: str

    @property
    def connect_address(self) -> str:
        if not self.addresses:
            raise ValueError("pinned target has no authorized address")
        return self.addresses[0]

    def public_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "addresses": list(self.addresses),
            "implied_port": self.implied_port,
            "source_sha256": self.source_sha256,
        }


_PINNED_TARGET: ContextVar[PinnedTarget | None] = ContextVar("friday_engineer_pinned_target", default=None)


@contextmanager
def bind_pinned_target(target: PinnedTarget | None) -> Iterator[PinnedTarget | None]:
    """Bind code-owned target authority across one model/tool turn."""

    token = _PINNED_TARGET.set(target)
    try:
        yield target
    finally:
        _PINNED_TARGET.reset(token)


def current_pinned_target() -> PinnedTarget | None:
    return _PINNED_TARGET.get()


def normalize_ip_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Collapse IPv4-mapped IPv6 before applying destination policy."""

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def is_forbidden_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Reject metadata aliases and non-target network classes fail-closed."""

    normalized = normalize_ip_address(address)
    if normalized in {_METADATA_V4, _METADATA_V6, *_OTHER_METADATA_V4}:
        return True
    return bool(
        normalized.is_unspecified
        or normalized.is_multicast
        or normalized.is_reserved
        or normalized.is_link_local
    )


def _normalize_hostname(value: str) -> str:
    host = str(value or "").strip().rstrip(".").casefold()
    if not host or len(host) > 253 or "\x00" in host or any(char.isspace() for char in host):
        raise ValueError("host is empty or malformed")
    try:
        return str(normalize_ip_address(ipaddress.ip_address(host)))
    except ValueError:
        pass
    if host == "localhost":
        return host
    labels = host.split(".")
    if len(labels) < 2 or any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise ValueError("host is empty or malformed")
    return host


def _validate_port(port: int | None) -> int | None:
    if port is None:
        return None
    if isinstance(port, bool) or not 1 <= int(port) <= 65535:
        raise ValueError("port is not in 1..65535")
    return int(port)


def parse_host_token(value: str) -> tuple[str, int | None]:
    raw = _TRAILING.sub("", str(value or "").strip())
    if not raw:
        raise ValueError("host is empty")
    if "://" in raw:
        parsed = urlsplit(raw)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("URL port is invalid") from exc
        return _normalize_hostname(parsed.hostname or ""), _validate_port(port)
    if raw.count(":") == 1 and not raw.startswith("["):
        host, maybe_port = raw.rsplit(":", 1)
        if maybe_port.isdigit():
            return _normalize_hostname(host), _validate_port(int(maybe_port))
    if raw.startswith("[") and "]" in raw:
        host = raw[1 : raw.index("]")]
        rest = raw[raw.index("]") + 1 :]
        if rest:
            if not rest.startswith(":") or not rest[1:].isdigit():
                raise ValueError("bracketed host has an invalid port")
            return _normalize_hostname(host), _validate_port(int(rest[1:]))
        return _normalize_hostname(host), None
    return _normalize_hostname(raw), None


def extract_targets(speech: str) -> list[dict[str, str | int | None]]:
    """Return unquoted current-speech targets in textual appearance order.

    Quoted examples, code and Markdown blockquotes are data, not destination
    authority.  Their bytes are position-preservingly blanked by the same
    projection used by the action classifier before any target regex runs.
    """

    _, authority = _request_projection(speech)
    candidates: list[tuple[int, int, int, str, str]] = []
    for priority, (kind, pattern) in enumerate(
        (
            ("url", _URL),
            ("ipv6", _IPV6_LOOSE),
            ("ipv4", _IPV4),
            ("ipv6", _IPV6),
            ("hostname", _HOSTNAME),
        )
    ):
        for match in pattern.finditer(authority):
            candidates.append((match.start(), match.end(), priority, kind, match.group()))
    candidates.sort(key=lambda item: (item[0], item[2], -(item[1] - item[0])))

    accepted_ranges: list[tuple[int, int]] = []
    found: list[dict[str, str | int | None]] = []
    seen: set[tuple[str, int | None]] = set()
    for start, end, _priority, kind, token in candidates:
        if any(
            start < accepted_end and end > accepted_start for accepted_start, accepted_end in accepted_ranges
        ):
            continue
        try:
            host, port = parse_host_token(token)
        except ValueError:
            continue
        key = (host, port)
        if key in seen:
            continue
        seen.add(key)
        accepted_ranges.append((start, end))
        found.append({"host": host, "port": port, "kind": kind, "token": token[:253]})
    return found


def extract_single_target(speech: str) -> dict[str, str | int | None] | None:
    """Select exactly one current-speech target or refuse the ambiguous turn."""

    targets = extract_targets(speech)
    if not targets:
        return None
    if len(targets) != 1:
        raise ValueError("engineer network turn must name exactly one target")
    return targets[0]


def extract_single_cidr(speech: str) -> str | None:
    """Return one unquoted canonical CIDR and reject mixed target authority."""

    _, authority = _request_projection(speech)
    url_ranges = [(item.start(), item.end()) for item in _URL.finditer(authority)]
    matches = [
        item
        for item in _CIDR.finditer(authority)
        if not any(item.start() < end and item.end() > start for start, end in url_ranges)
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("engineer network turn must name exactly one CIDR")
    match = matches[0]
    token = match.group()
    masked = authority[: match.start()] + (" " * len(token)) + authority[match.end() :]
    if extract_targets(masked):
        raise ValueError("engineer network turn cannot mix a CIDR with another target")
    try:
        network = ipaddress.ip_network(token, strict=True)
    except ValueError as exc:
        raise ValueError("engineer network CIDR is invalid or noncanonical") from exc
    canonical = str(network)
    if token.casefold() != canonical.casefold():
        raise ValueError("engineer network CIDR is invalid or noncanonical")
    return canonical


@dataclass(frozen=True, slots=True)
class _DirectRequestSpan:
    start: int
    end: int
    unit_start: int
    unit_end: int


def _normalize_request_text(speech: str) -> str:
    """Normalize words while retaining newline authority boundaries."""

    normalized = unicodedata.normalize("NFKC", str(speech or ""))
    return "\n".join(" ".join(line.split()) for line in normalized.splitlines())


def _mask_request_data(text: str) -> str:
    """Blank quoted/code/reported Markdown payloads without moving offsets."""

    masked = text
    for pattern in (
        _REQUEST_CODE_TEXT,
        _QUOTED_REQUEST_TEXT,
        _UNTERMINATED_REQUEST_QUOTE,
        _REQUEST_BLOCKQUOTE,
    ):
        masked = pattern.sub(lambda match: " " * len(match.group(0)), masked)
    return masked


def _request_projection(speech: str) -> tuple[str, str]:
    text = _normalize_request_text(speech)
    return text, _mask_request_data(text)


def _request_units(masked: str) -> Iterator[tuple[int, int]]:
    cursor = 0
    for boundary in _REQUEST_UNIT_BOUNDARY.finditer(masked):
        if cursor < boundary.start():
            yield cursor, boundary.start()
        cursor = boundary.end()
    if cursor < len(masked):
        yield cursor, len(masked)


def _request_is_negated(masked: str) -> bool:
    # Russian ``не мог бы`` is a conventional polite request, not a
    # cancellation. A second ``не`` before the action remains visible and denies it.
    negation_surface = _POLITE_NEGATIVE_MODAL.sub(
        lambda match: " " * len(match.group(0)),
        masked,
    )
    return _ACTIVE_ASSESSMENT_NEGATION.search(negation_surface) is not None


def _newline_payload_has_inert_governor(masked: str, unit_start: int) -> bool:
    """Keep a reported/example paragraph inert after a ``:`` + newline."""

    if _EXPLICIT_REQUEST_CONTEXT_RESET.match(masked[unit_start:]):
        return False
    prefix = masked[:unit_start]
    lines = prefix.split("\n")
    # The last item is the current (possibly empty) line prefix.  Only a
    # completed preceding line can introduce the following payload.  A blank
    # line does not end that authority boundary: pasted/report payloads can
    # contain arbitrary vertical whitespace.  A short ``Label:`` is also inert
    # because speaker attribution must never become packet authority.
    return any(
        line.rstrip().endswith(":")
        and (_REPORTED_REQUEST_CUE.search(line) or _META_REQUEST_CUE.search(line) or len(line.strip()) <= 96)
        for line in lines[:-1]
    )


def _direct_request_matches(speech: str, pattern: re.Pattern[str]) -> tuple[_DirectRequestSpan, ...]:
    """Locate direct action clauses and keep data/reported speech inert.

    A fact may precede the request (``nmap is installed, scan ...``), but the
    action itself must begin a punctuation-delimited clause.  Every governing
    prefix in the same sentence is inspected, so inserting ``please`` between a
    reporting verb and its quoted command cannot mint effect authority.
    """

    _text, masked = _request_projection(speech)
    if not masked.strip():
        return ()
    found: list[_DirectRequestSpan] = []
    for unit_start, unit_end in _request_units(masked):
        if _newline_payload_has_inert_governor(masked, unit_start):
            continue
        unit = masked[unit_start:unit_end]
        if _CONDITIONAL_REQUEST_CUE.search(unit):
            continue
        starts = [unit_start]
        starts.extend(unit_start + boundary.end() for boundary in _REQUEST_SOFT_BOUNDARY.finditer(unit))
        for start in starts:
            request = pattern.match(masked[start:unit_end])
            if request is None:
                continue
            governing_prefix = masked[unit_start:start]
            if _REPORTED_REQUEST_CUE.search(governing_prefix) or _META_REQUEST_CUE.search(governing_prefix):
                continue
            request_start = start + request.start()
            request_end = start + request.end()
            trailing = masked[request_end:]
            if _TRAILING_REQUEST_CANCEL.search(trailing) or _TRAILING_REQUEST_ATTRIBUTION.search(trailing):
                return ()
            found.append(
                _DirectRequestSpan(
                    start=request_start,
                    end=request_end,
                    unit_start=unit_start,
                    unit_end=unit_end,
                )
            )
            break
    return tuple(found)


def requests_active_assessment(speech: str) -> bool:
    """Return whether the current human text explicitly asks for active probes.

    A host or URL is data, not effect authority.  This deliberately narrow,
    code-owned gate admits only direct request language and fails closed on a
    negated request.  Target extraction and policy admission remain separate
    gates; this predicate alone can never authorize a destination.
    """

    text, masked = _request_projection(speech)
    if not text or _request_is_negated(masked):
        return False
    request_spans = _direct_request_matches(text, _ACTIVE_ASSESSMENT_VERB)
    if not request_spans:
        return False
    targets = extract_targets(text)
    if len(targets) != 1:
        # Preserve an explicit zero/multi-target request for the separate exact
        # target gate, which will return a useful refusal without doing DNS.
        return any(
            _PASSIVE_ASSESSMENT_OBJECT.search(masked[item.unit_start : item.unit_end]) is None
            for item in request_spans
        )
    token = str(targets[0].get("token") or "")
    target_start = text.casefold().find(token.casefold())
    if target_start < 0:
        return False
    target_end = target_start + len(token)
    for request_span in request_spans:
        between = (
            masked[request_span.end : target_start]
            if request_span.end <= target_start
            else masked[target_end : request_span.start]
        )
        # “Inspect the report about host” is a request to inspect passive
        # material, not permission to contact the host. Keep the effect phrase
        # close to the target and free of an intervening passive object.
        if len(between) <= 160 and _PASSIVE_ASSESSMENT_OBJECT.search(between) is None:
            return True
    return False


def requests_artifact_patch(speech: str) -> bool:
    """Admit artifact mutation only from a direct, current-human request.

    Static evidence is adversarial and may tell the model to call the patch
    tool.  This narrow code-owned predicate is evaluated only against the
    authenticated current user message; an uploaded file, prior conversation,
    dossier text, or model output cannot satisfy it.
    """

    text = " ".join(str(speech or "").split())
    return bool(
        text
        and _ARTIFACT_PATCH_NEGATION.search(text) is None
        and _ARTIFACT_PATCH_REQUEST.search(text) is not None
    )


def requests_configured_network_assessment(speech: str) -> bool:
    """Admit the sole configured private network only from current speech.

    This is the code-owned meaning of “scan my subnet”.  Passive reports and
    configuration questions never authorize packets; an ambiguous configured
    scope is rejected later by policy rather than selected by a model.
    """

    text, masked = _request_projection(speech)
    if not requests_network_scan(text) or extract_targets(text):
        return False
    requests = _direct_request_matches(text, _CONFIGURED_NETWORK_ACTIVE_VERB)
    if not requests:
        requests = _direct_request_matches(text, _ACTIVE_ASSESSMENT_VERB)
    return any(
        _CONFIGURED_NETWORK_OBJECT.search(masked[item.start : item.unit_end]) is not None for item in requests
    )


def requests_network_scan(speech: str) -> bool:
    """Require explicit packet-intent language for a CIDR-wide effect."""

    text, masked = _request_projection(speech)
    if not requests_active_assessment(text):
        return False
    packet_requests = _direct_request_matches(text, _CONFIGURED_NETWORK_ACTIVE_VERB)
    if any(
        _PASSIVE_ASSESSMENT_OBJECT.search(masked[item.unit_start : item.unit_end]) is None
        for item in packet_requests
    ):
        return True
    # A generic “check” is packet authority only when the current clause also
    # names an explicit scanner.  “Check my network/config/password” stays
    # passive and cannot reach nmap.
    return any(
        _PASSIVE_ASSESSMENT_OBJECT.search(masked[item.start : item.unit_end]) is None
        and _NETWORK_SCAN_MECHANISM.search(masked[item.start : item.unit_end])
        for item in _direct_request_matches(text, _ACTIVE_ASSESSMENT_VERB)
    )


def target_source_sha256(speech: str, token: str) -> str:
    body = f"{str(speech or '')}\x00{str(token or '')}".encode("utf-8", errors="replace")
    return hashlib.sha256(body).hexdigest()


__all__ = [
    "PinnedTarget",
    "bind_pinned_target",
    "current_pinned_target",
    "extract_single_target",
    "extract_single_cidr",
    "extract_targets",
    "is_forbidden_address",
    "normalize_ip_address",
    "parse_host_token",
    "requests_active_assessment",
    "requests_artifact_patch",
    "requests_configured_network_assessment",
    "requests_network_scan",
    "target_source_sha256",
]
