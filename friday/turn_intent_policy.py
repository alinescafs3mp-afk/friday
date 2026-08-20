"""Deterministic policy for weather, capability meta, and local diagnostics turns.

The module is intentionally pure: it does not import runtime, storage, web, or
authorization services.  A caller supplies the exact capability result needed
for diagnostics and executes the selected action at its normal boundary.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

ADMIN_DIAGNOSTICS_CAPABILITY = "admin.diagnostics"
WEATHER_LOCATION_CLARIFICATION = "Для какого города или населённого пункта нужен прогноз?"
WEATHER_LOCATION_CHALLENGE_RESPONSE = (
    "В предыдущем запросе город не был указан. Я не использую геолокацию и не должна "
    "выбирать город сама. Для какого города или населённого пункта нужен прогноз?"
)
_MAX_CLASSIFICATION_CHARS = 4096
_MAX_DIAGNOSTIC_ACTIONS = 256
_MAX_DIAGNOSTIC_RENDER_CHARS = 640
_MAX_ALLOWLISTED_TOOL_COUNT = 65_535


class TurnIntent(str, Enum):
    """A small code-owned intent set handled before model routing."""

    PASSTHROUGH = "passthrough"
    WEATHER_NEEDS_LOCATION = "weather_needs_location"
    WEATHER_LOCATION_CHALLENGE = "weather_location_challenge"
    WEATHER_WITH_LOCATION = "weather_with_location"
    META_CAPABILITIES = "meta_capabilities"
    META_INTEGRATIONS = "meta_integrations"
    LOCAL_DIAGNOSTICS = "local_diagnostics"
    LOCAL_DIAGNOSTICS_DENIED = "local_diagnostics_denied"


class WebDisposition(str, Enum):
    """Whether this policy changes the ordinary web-routing decision."""

    UNCHANGED = "unchanged"
    DENY = "deny"
    ALLOW_EXPLICIT_WEATHER = "allow_explicit_weather"


class AttachmentDisposition(str, Enum):
    """Whether ambient/restored attachments may enter the handled turn."""

    UNCHANGED = "unchanged"
    NONE = "none"


class LocationSource(str, Enum):
    """The only location provenance accepted by this policy."""

    EXPLICIT_USER_TEXT = "explicit_user_text"


class WeatherHorizon(str, Enum):
    """Closed temporal scope that may cross an adjacent weather clarification."""

    CURRENT = "current"
    TODAY = "today"
    TOMORROW = "tomorrow"

    @property
    def query_token_ru(self) -> str:
        return {
            WeatherHorizon.CURRENT: "сейчас",
            WeatherHorizon.TODAY: "сегодня",
            WeatherHorizon.TOMORROW: "завтра",
        }[self]


class DiagnosticsState(str, Enum):
    """Closed, non-sensitive state vocabulary for a public diagnostic result."""

    READY = "ready"
    ATTENTION = "attention"
    SETUP_REQUIRED = "setup_required"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CapabilityProjection:
    """Code-owned Proposal85 facts about the caller's own accepted material."""

    schema: str = "friday.public-capabilities.v1"
    local_state: bool = True
    accepted_own_chat_rows_durable: bool = True
    accepted_own_chat_rows_deletable: bool = False
    prompt_window_is_full_history: bool = False
    message_search_reads_full_own_history: bool = True
    message_search_paginated: bool = True
    accepted_own_history_complete_via_pagination: bool = True
    accepted_files_persisted: bool = True
    file_search_supported: bool = True
    access_scoped: bool = True

    def render_ru(self) -> str:
        """Render the code-owned projection without paths or private counts."""

        return (
            "Friday сохраняет каждое успешно принятое сообщение этого чата в долговечной "
            "неудаляемой строке. В рабочий промпт попадает только короткое актуальное окно, "
            "а message_search читает всю принятую историю этого чата постранично. Успешно "
            "принятые файлы тоже сохраняются и доступны поиску в пределах прав. Полнота "
            "конкретной выдачи зависит от успешного приёма, области доступа и пагинации."
        )


CODE_OWNED_CAPABILITY_PROJECTION = CapabilityProjection()


@dataclass(frozen=True, slots=True)
class IntegrationProjection:
    """Externally observed MCP facts with no provider names, paths, or payloads."""

    mcp_configured: bool
    mcp_connected: bool
    allowlisted_tool_count: int
    schema: ClassVar[str] = "friday.integration-projection.v1"

    def __post_init__(self) -> None:
        if type(self.mcp_configured) is not bool or type(self.mcp_connected) is not bool:
            raise ValueError("MCP state facts must be exact booleans")
        if (
            isinstance(self.allowlisted_tool_count, bool)
            or not isinstance(self.allowlisted_tool_count, int)
            or not 0 <= self.allowlisted_tool_count <= _MAX_ALLOWLISTED_TOOL_COUNT
        ):
            raise ValueError("allowlisted MCP tool count is invalid")
        if self.mcp_connected and not self.mcp_configured:
            raise ValueError("connected MCP must also be configured")
        if not self.mcp_configured and self.allowlisted_tool_count:
            raise ValueError("disabled MCP cannot expose allowlisted tools")

    def render_ru(self) -> str:
        """Render the three code-owned states without inventing integration facts."""

        if not self.mcp_configured:
            return "MCP для этой установки не настроен."
        if not self.mcp_connected:
            return (
                "MCP настроен, но соединение сейчас недоступно. "
                f"В конфигурации разрешено MCP-инструментов: {self.allowlisted_tool_count}."
            )
        return f"MCP подключён. Доступно разрешённых MCP-инструментов: {self.allowlisted_tool_count}."


@dataclass(frozen=True, slots=True)
class SafeDiagnosticsProjection:
    """Bounded projection that cannot retain raw diagnostic fields or secrets."""

    state: DiagnosticsState
    errors: int = 0
    warnings: int = 0
    setup_actions: int = 0
    other_actions: int = 0
    actions_truncated: bool = False
    source_valid: bool = True
    schema: str = "friday.safe-diagnostics-projection.v1"

    def __post_init__(self) -> None:
        for value in (self.errors, self.warnings, self.setup_actions, self.other_actions):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _MAX_DIAGNOSTIC_ACTIONS
            ):
                raise ValueError("diagnostic action counts must be bounded integers")
        if not isinstance(self.state, DiagnosticsState):
            raise ValueError("diagnostic state must use the closed vocabulary")
        if type(self.actions_truncated) is not bool or type(self.source_valid) is not bool:
            raise ValueError("diagnostic validity flags must be booleans")
        if self.schema != "friday.safe-diagnostics-projection.v1":
            raise ValueError("diagnostic projection schema must be code-owned")

    def render_ru(self) -> str:
        """Render only fixed labels and bounded counts, never raw report values."""

        state_label = {
            DiagnosticsState.READY: "готова",
            DiagnosticsState.ATTENTION: "требует внимания",
            DiagnosticsState.SETUP_REQUIRED: "требует настройки",
            DiagnosticsState.DEGRADED: "есть отказ",
            DiagnosticsState.UNKNOWN: "состояние не подтверждено",
        }[self.state]
        result = (
            f"Локальная диагностика: {state_label}. "
            f"Действия: ошибок — {self.errors}, предупреждений — {self.warnings}, "
            f"настройка — {self.setup_actions}, прочих — {self.other_actions}."
        )
        if self.actions_truncated or not self.source_valid:
            result += " Проекция неполна; подробности доступны только в административной диагностике."
        return result[:_MAX_DIAGNOSTIC_RENDER_CHARS]


def project_safe_diagnostics(report: Mapping[str, object]) -> SafeDiagnosticsProjection:
    """Drop raw diagnostic content and retain only a strict bounded summary."""

    raw_state = report.get("state")
    try:
        state = DiagnosticsState(raw_state) if isinstance(raw_state, str) else DiagnosticsState.UNKNOWN
    except ValueError:
        state = DiagnosticsState.UNKNOWN

    raw_actions = report.get("actions")
    if not isinstance(raw_actions, list):
        return SafeDiagnosticsProjection(state=DiagnosticsState.UNKNOWN, source_valid=False)

    errors = warnings = setup_actions = other_actions = 0
    for action in raw_actions[:_MAX_DIAGNOSTIC_ACTIONS]:
        severity = action.get("severity") if isinstance(action, Mapping) else None
        if severity == "error":
            errors += 1
        elif severity == "warning":
            warnings += 1
        elif severity == "setup":
            setup_actions += 1
        else:
            other_actions += 1
    truncated = len(raw_actions) > _MAX_DIAGNOSTIC_ACTIONS
    source_valid = state is not DiagnosticsState.UNKNOWN and other_actions == 0 and not truncated
    if not source_valid:
        state = DiagnosticsState.UNKNOWN
    return SafeDiagnosticsProjection(
        state=state,
        errors=errors,
        warnings=warnings,
        setup_actions=setup_actions,
        other_actions=other_actions,
        actions_truncated=truncated,
        source_valid=source_valid,
    )


@dataclass(frozen=True, slots=True)
class DiagnosticsAuthority:
    """Exact capability result obtained at the authorization boundary."""

    capability_allowed: bool = False

    def __post_init__(self) -> None:
        if type(self.capability_allowed) is not bool:
            raise ValueError("capability decision must be an exact boolean")

    @property
    def allowed(self) -> bool:
        return self.capability_allowed


@dataclass(frozen=True, slots=True)
class TurnPolicyContext:
    """Minimal adjacent-turn state; it deliberately contains no ambient location."""

    weather_followup: bool = False
    weather_horizon: WeatherHorizon | None = None
    weather_location_missing: bool = False

    def __post_init__(self) -> None:
        if type(self.weather_followup) is not bool:
            raise ValueError("weather follow-up marker must be an exact boolean")
        if self.weather_horizon is not None and not isinstance(
            self.weather_horizon,
            WeatherHorizon,
        ):
            raise ValueError("weather horizon must use the closed vocabulary")
        if self.weather_horizon is not None and not self.weather_followup:
            raise ValueError("weather horizon requires an adjacent weather turn")
        if type(self.weather_location_missing) is not bool:
            raise ValueError("weather location marker must be an exact boolean")
        if self.weather_location_missing and not self.weather_followup:
            raise ValueError("missing weather location requires an adjacent weather turn")


@dataclass(frozen=True, slots=True)
class TurnPolicyDecision:
    """Immutable result consumed later by the runtime integration."""

    intent: TurnIntent
    web: WebDisposition = WebDisposition.UNCHANGED
    attachments: AttachmentDisposition = AttachmentDisposition.UNCHANGED
    location: str | None = None
    location_source: LocationSource | None = None
    weather_horizon: WeatherHorizon | None = None
    public_response: str | None = None
    capability_projection: CapabilityProjection | None = None
    integration_projection: IntegrationProjection | None = None
    required_capability: str | None = None
    local_diagnostics_allowed: bool = False

    def __post_init__(self) -> None:
        if self.weather_horizon is not None and not isinstance(
            self.weather_horizon,
            WeatherHorizon,
        ):
            raise ValueError("weather horizon must use the closed vocabulary")

    @property
    def handled(self) -> bool:
        return self.intent is not TurnIntent.PASSTHROUGH


_WEATHER_CUE = re.compile(
    r"(?:^|\W)(?:погод\w*|прогноз\w*|weather|forecast)(?:$|\W)",
    re.IGNORECASE,
)
_WEATHER_LOCATION = re.compile(
    r"(?=(?:^|\W)(?:в|во|для|in|for)\s+"
    r"(?P<location>[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё.-]*"
    r"(?:\s+[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё.-]*){0,3}))",
    re.IGNORECASE,
)
_LOCATION_WORD = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё.-]*$")
# A bare adjacent answer is useful after the code-owned location question, but
# shape alone is not enough: ordinary acknowledgements ("спасибо"), refusals
# ("нет, не надо") and generic place substitutes ("в интернете") are
# made only of valid letters too.  Keep this closed ambiguity vocabulary at the
# policy boundary.  Unknown proper names remain accepted; known non-locations
# fail back to ordinary dialogue instead of opening the web route.
_AMBIGUOUS_LOCATION_WORDS = frozenset(
    {
        "a",
        "and",
        "cancel",
        "for",
        "home",
        "in",
        "internet",
        "later",
        "network",
        "no",
        "not",
        "office",
        "ok",
        "okay",
        "online",
        "please",
        "thanks",
        "thankyou",
        "work",
        "а",
        "благодарю",
        "в",
        "во",
        "все",
        "всё",
        "да",
        "давай",
        "дальше",
        "для",
        "дом",
        "дома",
        "значит",
        "интернет",
        "интернете",
        "интернету",
        "ладно",
        "мир",
        "мире",
        "на",
        "надо",
        "не",
        "ненадо",
        "нет",
        "ок",
        "окей",
        "онлайн",
        "офис",
        "офисе",
        "позже",
        "понял",
        "поняла",
        "понятно",
        "работа",
        "работе",
        "сети",
        "сеть",
        "спасибо",
        "хватит",
        "хорошо",
        "ясно",
    }
)
_LOCATION_TAIL_STOP = frozenset(
    {
        "а",
        "and",
        "afternoon",
        "воскресенье",
        "воскресенья",
        "воскресенью",
        "evening",
        "вечером",
        "выходные",
        "выходных",
        "днем",
        "днём",
        "день",
        "дня",
        "завтра",
        "какая",
        "какие",
        "какое",
        "какой",
        "monday",
        "me",
        "местности",
        "меня",
        "мне",
        "моего",
        "моем",
        "моём",
        "мой",
        "мою",
        "на",
        "наш",
        "нашего",
        "нашем",
        "нашём",
        "нас",
        "неделю",
        "неделе",
        "ночь",
        "ночью",
        "now",
        "понедельник",
        "понедельника",
        "понедельнике",
        "пожалуйста",
        "позже",
        "please",
        "пятница",
        "пятницу",
        "пятницы",
        "пятнице",
        "saturday",
        "сейчас",
        "среда",
        "среду",
        "среды",
        "среде",
        "суббота",
        "субботу",
        "субботы",
        "субботе",
        "sunday",
        "сегодня",
        "thursday",
        "today",
        "tomorrow",
        "tonight",
        "тут",
        "tuesday",
        "you",
        "здесь",
        "город",
        "города",
        "городе",
        "городом",
        "рядом",
        "вас",
        "вам",
        "тебя",
        "тебе",
        "утро",
        "утром",
        "вечер",
        "вторник",
        "вторника",
        "вторнике",
        "wednesday",
        "четверг",
        "четверга",
        "четверге",
        "friday",
    }
)
_CORRECTION_PATTERNS = (
    re.compile(
        r"^меня\s+(?P<location>.+?)\s+интересовал(?:а|о|и)?[.!?)]*$",
        re.IGNORECASE,
    ),
    re.compile(r"^я\s+(?:про|о)\s+(?P<location>.+?)[.!?)]*$", re.IGNORECASE),
    re.compile(
        r"^(?:я\s+имел(?:а)?\s+в\s+виду|нужен|нужна)\s+(?P<location>.+?)[.!?)]*$",
        re.IGNORECASE,
    ),
)
_WHY_LOCATION = re.compile(r"^\s*(?:а\s+)?почему\b", re.IGNORECASE)
_WEATHER_HORIZONS: tuple[tuple[WeatherHorizon, re.Pattern[str]], ...] = (
    (WeatherHorizon.TOMORROW, re.compile(r"(?:^|\W)(?:завтра|tomorrow)(?:$|\W)", re.IGNORECASE)),
    (WeatherHorizon.TODAY, re.compile(r"(?:^|\W)(?:сегодня|today)(?:$|\W)", re.IGNORECASE)),
    (
        WeatherHorizon.CURRENT,
        re.compile(r"(?:^|\W)(?:сейчас|now|current(?:ly)?|текущ\w*)(?:$|\W)", re.IGNORECASE),
    ),
)

_DIAGNOSTICS = re.compile(
    r"(?:самодиагност\w*|(?:диагностик\w*|провер\w*\s+состояни\w*)\s+"
    r"(?:систем\w*|сервис\w*|пятниц\w*|себя|сво\w*))",
    re.IGNORECASE,
)

_DATA_READ_COMMAND = re.compile(
    r"(?:^|\W)(?:вывед\w*|выгруз\w*|дай\s+(?:список|сводку)|найд\w*|обобщ\w*|"
    r"открой\w*|перечисл\w*|покаж\w*|прочит\w*|процитир\w*|удал\w*)(?:$|\W)",
    re.IGNORECASE,
)
_DATA_READ_SCOPE = re.compile(
    r"(?:сообщени\w*|переписк\w*|истори\w*|чат\w*|"
    r"файл(?:а|е|у|ы|ов|ом|ами|ах)?\b|документ\w*|вложени\w*|архив\w*|"
    r"запис(?:ь|и|ей|ью|ям|ями|ях)\b|отч[её]т\w*|\.[A-Za-z0-9]{1,8}\b)",
    re.IGNORECASE,
)
_DATA_READ_PROSE = re.compile(
    r"(?:что\s+(?:написан\w*|сказан\w*|был\w*)|где\s+(?:я|мы)\s+писал\w*|"
    r"(?:я|мы)\s+(?:писал\w*|обсуждал\w*|упоминал\w*)|"
    r"(?:писал\w*|обсуждал\w*|упоминал\w*|говорил\w*|написан\w*|сказан\w*|был\w*))",
    re.IGNORECASE,
)
_SENT_OBJECT_REFERENCE = re.compile(
    r"котор\w*\s+(?:я|мы)\s+(?:тебе\s+)?(?:отправ\w*|загруз\w*|присыл\w*)",
    re.IGNORECASE,
)
_DATA_SUBJECT_RELATION = re.compile(
    r"(?:"
    r"\b(?:ка(?:кая|кой|кие)|что|где)?\s*[^.!?\n]{0,100}?\b"
    r"(?:указан\w*|содерж\w*|описан\w*)?\s*(?:в|во|из)\s+"
    r"(?:(?:прислан|отправлен|загружен|прикрепл[её]н)\w*\s+)?"
    r"(?:отч[её]т|файл|документ|вложени)\w*\b|"
    r"\b(?:в|во|из)\s+(?:(?:прислан|отправлен|загружен|прикрепл[её]н)\w*\s+)?"
    r"(?:отч[её]т|файл|документ|вложени)\w*\b[^.!?\n]{0,100}?"
    r"(?:указан\w*|содерж\w*|описан\w*)\b"
    r")",
    re.IGNORECASE,
)
_META_CAPABILITY = re.compile(
    r"(?:"
    r"(?:ты|friday|пятниц\w*)\s+(?:вед[её]шь|записыва\w*|помни\w*|сохраня\w*|храни\w*)"
    r"|у\s+тебя\s+есть\s+(?:локальн\w+\s+)?(?:sqlite|баз\w*|хранилищ\w*)"
    r"|(?:где|как|что)\s+(?:ты\s+)?(?:записыва\w*|сохраня\w*|храни\w*)"
    r"|(?:можешь|умеешь|поддержива\w*)\s+(?:ли\s+)?(?:ты\s+)?искать"
    r"|доступен\s+ли\s+(?:тебе\s+)?поиск"
    r")",
    re.IGNORECASE,
)
_META_OBJECT = re.compile(
    r"(?:sqlite|баз\w*|истори\w*\s+переписк\w*|сообщени\w*|переписк\w*|"
    r"файл\w*|вложени\w*|хранилищ\w*)",
    re.IGNORECASE,
)
_MCP_META = re.compile(
    r"(?:"
    r"у\s+тебя\s+(?:есть|подключ[её]н|настроен)\s+mcp"
    r"|у\s+тебя\s+mcp\s+(?:есть|подключ[её]н|настроен)"
    r"|есть\s+ли\s+у\s+тебя\s+mcp"
    r"|mcp\s+у\s+тебя\s+(?:есть|подключ[её]н|настроен)"
    r"|(?:какие|сколько)\s+(?:тебе\s+)?mcp(?:[-\s]+(?:инструмент\w*|сервер\w*))?\s+"
    r"(?:тебе\s+)?(?:доступн\w*|подключ\w*|настроен\w*)"
    r"|mcp(?:[-\s]+(?:инструмент\w*|сервер\w*))?\s+какие\s+(?:тебе\s+)?"
    r"(?:доступн\w*|подключ\w*|настроен\w*)"
    r")",
    re.IGNORECASE,
)


def _bounded_text(message: str) -> str:
    return " ".join(str(message or "")[:_MAX_CLASSIFICATION_CHARS].split()).strip()


def _clean_location(candidate: str) -> str | None:
    words = [word.strip(" .,!?:;()[]{}«»\"'") for word in candidate.split()]
    clean: list[str] = []
    for word in words:
        folded = word.casefold().replace("ё", "е")
        if not word or folded in _LOCATION_TAIL_STOP or folded in _AMBIGUOUS_LOCATION_WORDS:
            break
        if not _LOCATION_WORD.fullmatch(word):
            break
        clean.append(word)
    if not clean or len(clean) > 4:
        return None
    return " ".join(clean)


def _explicit_weather_location(text: str) -> str | None:
    if _WEATHER_CUE.search(text) is None:
        return None
    for match in _WEATHER_LOCATION.finditer(text):
        location = _clean_location(match.group("location"))
        if location is not None:
            return location
    return None


def _adjacent_weather_correction(text: str) -> str | None:
    if _WHY_LOCATION.search(text):
        return None
    for pattern in _CORRECTION_PATTERNS:
        match = pattern.fullmatch(text)
        if match is not None:
            return _clean_location(match.group("location"))
    # Commas/semicolons/colons inside a bare answer are discourse, not a proper
    # name boundary. Explicit correction forms above remain free to contain
    # their normal terminal punctuation.
    bare = text.strip()
    if any(mark in bare for mark in (",", ";", ":")):
        return None
    bare = bare.strip(" .!?()[]{}«»\"'")
    words = bare.split()
    if words and words[0].casefold() in {"в", "во", "in"}:
        words = words[1:]
    if 1 <= len(words) <= 4:
        return _clean_location(" ".join(words))
    return None


def _weather_horizon(text: str) -> WeatherHorizon | None:
    for horizon, pattern in _WEATHER_HORIZONS:
        if pattern.search(text) is not None:
            return horizon
    return None


def _is_meta_capability_question(text: str) -> bool:
    return bool(
        not _DATA_READ_COMMAND.search(text) and _META_CAPABILITY.search(text) and _META_OBJECT.search(text)
    )


def _is_data_read_request(text: str) -> bool:
    """Keep quoted/history/file subjects out of live system intents.

    Weather and diagnostics words may be the *object being retrieved*, not an
    instruction to call weather or inspect this process.  Only the regular
    retrieval runtime owns those scoped requests and its authorization gates.
    """

    return bool(
        (_DATA_READ_SCOPE.search(text) and (_DATA_READ_COMMAND.search(text) or _DATA_READ_PROSE.search(text)))
        or _SENT_OBJECT_REFERENCE.search(text)
        or _DATA_SUBJECT_RELATION.search(text)
    )


def decide_turn_policy(
    message: str,
    *,
    context: TurnPolicyContext | None = None,
    diagnostics: DiagnosticsAuthority | None = None,
    integrations: IntegrationProjection | None = None,
) -> TurnPolicyDecision:
    """Return a deterministic, immutable pre-routing decision.

    Only an explicit location span in ``message`` can authorize a weather web
    route.  Neither this API nor its context accepts IP/profile/geolocation data.
    """

    text = _bounded_text(message)
    turn_context = context or TurnPolicyContext()
    authority = diagnostics or DiagnosticsAuthority()

    if _is_data_read_request(text):
        return TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH)

    if _DIAGNOSTICS.search(text):
        if authority.allowed:
            return TurnPolicyDecision(
                intent=TurnIntent.LOCAL_DIAGNOSTICS,
                attachments=AttachmentDisposition.NONE,
                web=WebDisposition.DENY,
                required_capability=ADMIN_DIAGNOSTICS_CAPABILITY,
                local_diagnostics_allowed=True,
            )
        return TurnPolicyDecision(
            intent=TurnIntent.LOCAL_DIAGNOSTICS_DENIED,
            attachments=AttachmentDisposition.NONE,
            web=WebDisposition.DENY,
            required_capability=ADMIN_DIAGNOSTICS_CAPABILITY,
            public_response="Локальная самодиагностика требует права admin.diagnostics.",
        )

    if _MCP_META.search(text):
        if integrations is None:
            return TurnPolicyDecision(
                intent=TurnIntent.META_INTEGRATIONS,
                web=WebDisposition.DENY,
                attachments=AttachmentDisposition.NONE,
                public_response=(
                    "Локальный менеджер интеграций не передал статус MCP; подтвердить "
                    "его конфигурацию и соединение сейчас нельзя."
                ),
            )
        return TurnPolicyDecision(
            intent=TurnIntent.META_INTEGRATIONS,
            web=WebDisposition.DENY,
            attachments=AttachmentDisposition.NONE,
            public_response=integrations.render_ru(),
            integration_projection=integrations,
        )

    if _is_meta_capability_question(text):
        projection = CODE_OWNED_CAPABILITY_PROJECTION
        return TurnPolicyDecision(
            intent=TurnIntent.META_CAPABILITIES,
            web=WebDisposition.DENY,
            attachments=AttachmentDisposition.NONE,
            public_response=projection.render_ru(),
            capability_projection=projection,
        )

    if _WEATHER_CUE.search(text):
        location = _explicit_weather_location(text)
        horizon = _weather_horizon(text)
        if location is None:
            return TurnPolicyDecision(
                intent=TurnIntent.WEATHER_NEEDS_LOCATION,
                web=WebDisposition.DENY,
                attachments=AttachmentDisposition.NONE,
                public_response=WEATHER_LOCATION_CLARIFICATION,
                weather_horizon=horizon,
            )
        return TurnPolicyDecision(
            intent=TurnIntent.WEATHER_WITH_LOCATION,
            web=WebDisposition.ALLOW_EXPLICIT_WEATHER,
            attachments=AttachmentDisposition.NONE,
            location=location,
            location_source=LocationSource.EXPLICIT_USER_TEXT,
            weather_horizon=horizon,
        )

    if turn_context.weather_followup:
        if turn_context.weather_location_missing and _WHY_LOCATION.search(text) is not None:
            return TurnPolicyDecision(
                intent=TurnIntent.WEATHER_LOCATION_CHALLENGE,
                web=WebDisposition.DENY,
                attachments=AttachmentDisposition.NONE,
                weather_horizon=turn_context.weather_horizon,
                public_response=WEATHER_LOCATION_CHALLENGE_RESPONSE,
            )
        location = _adjacent_weather_correction(text)
        if location is not None:
            return TurnPolicyDecision(
                intent=TurnIntent.WEATHER_WITH_LOCATION,
                web=WebDisposition.ALLOW_EXPLICIT_WEATHER,
                attachments=AttachmentDisposition.NONE,
                location=location,
                location_source=LocationSource.EXPLICIT_USER_TEXT,
                weather_horizon=turn_context.weather_horizon,
            )

    return TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH)


__all__ = [
    "ADMIN_DIAGNOSTICS_CAPABILITY",
    "AttachmentDisposition",
    "CODE_OWNED_CAPABILITY_PROJECTION",
    "CapabilityProjection",
    "DiagnosticsAuthority",
    "DiagnosticsState",
    "IntegrationProjection",
    "LocationSource",
    "SafeDiagnosticsProjection",
    "TurnIntent",
    "TurnPolicyContext",
    "TurnPolicyDecision",
    "WEATHER_LOCATION_CLARIFICATION",
    "WEATHER_LOCATION_CHALLENGE_RESPONSE",
    "WebDisposition",
    "WeatherHorizon",
    "decide_turn_policy",
    "project_safe_diagnostics",
]
