"""Bounded V12 reading of already registered exact-text files.

The selector is deliberately code-owned.  The model may classify a turn as
``archive_read`` but it cannot choose a person, Raw id or time boundary.  This
first historical slice accepts only an exact filename, a closed local calendar
window, or the latest one/two files.  Wider/ambiguous corpora retain the legacy
owner until the hierarchy evidence contract is promoted.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from typing import Any
from zoneinfo import ZoneInfo

from friday.file_evidence_reader import (
    FileEvidenceUnavailable,
    HistoricalFileSelectionToken,
    historical_file_selection_is_current,
    historical_file_selection_token,
    prepare_registered_file_evidence,
)
from friday.orchestration.contracts import RouteClass, TurnInput, TurnPlan
from friday.orchestration.file_read import V12FileReadHandler, _PreparedFileContext
from friday.orchestration.file_read_contract import archive_read_plan_supports_selection
from friday.orchestration.router import ReadOnlyRoutePreparation, ReadOnlyRouteRequest
from friday.storage import normalize_conversation_mode
from friday.time_routing import (
    TimeIntent,
    build_time_window,
    has_explicit_timezone,
    has_invalid_clock_expression,
    has_mixed_time_direction,
    has_multiple_time_targets,
    has_relational_clock_boundary,
    has_unsupported_time_granularity,
    lexical_time_window_kind,
    temporal_routing_text,
)

_MAX_ARCHIVE_FILES = 2
_EXPLICIT_PERSON = re.compile(
    r"(?<![\w@])(?P<handle>@[0-9A-Za-z_]{3,64})\b|"
    r"\b(?:пользовател|участник)\w*\s+(?P<name>[0-9A-Za-zА-ЯЁа-яё_.-]{2,64})\b",
    re.IGNORECASE,
)
_DOCUMENT_DATE = re.compile(
    r"\b(?:дат(?:а|е|ой)\s+документ\w*|документ\w*\s*[,;:-]?\s*датир\w*|"
    r"в\s+документ\w*\s+за)\b",
    re.IGNORECASE,
)
_RECEIVED_DATE = re.compile(
    r"\b(?:получ\w*|приходил\w*|поступал\w*|присыл\w*|присла\w*|"
    r"загруж\w*|отправл\w*|скидыв\w*|скинул\w*)\b",
    re.IGNORECASE,
)
_NEGATED_DOCUMENT_DATE = re.compile(
    r"\bне\s+(?:по\s+|использ\w*\s+)?(?:дат\w*\s+документ\w*|документ\w*\s+датир\w*)\b",
    re.IGNORECASE,
)
_LATEST = re.compile(
    r"\bпоследн\w*\s+(?P<count>[12]|один|одну|два|две)\s+"
    r"(?:файл|документ|материал|вложен)\w*\b",
    re.IGNORECASE,
)
_QUOTED_FILENAME = re.compile(
    r"\b(?:файл|документ|вложен)\w*\s+"
    r"(?:[«\"`](?P<quoted>[^»\"`\r\n]{1,260})[»\"`]|(?P<plain>[^\s,;!?]{1,260}\.[0-9A-Za-z]{1,12}))",
    re.IGNORECASE,
)
_FILENAME_TOKEN = re.compile(r"(?<![\w@])@?[0-9A-Za-zА-ЯЁа-яё_.-]{1,247}\.[0-9A-Za-z]{1,12}(?![\w.])")
_ARCHIVE_ACTION = (
    r"(?:обобщи|обобщите|суммируй|суммируйте|суммаризируй|суммаризируйте|"
    r"прочитай|прочитайте|покажи|покажите|выведи|выведите|найди|найдите|"
    r"расскажи|расскажите|перечисли|перечислите|дай|дайте)"
)
_ARCHIVE_SOURCE = (
    r"(?:файл|файлы|файла|файлов|документ|документы|документа|документов|"
    r"материал|материалы|материала|материалов|вложение|вложения|вложений|"
    r"отч[её]т|отч[её]ты|отч[её]та|отч[её]тов)"
)
_ARCHIVE_LEAD = (
    rf"(?:пожалуйста\s+)?{_ARCHIVE_ACTION}(?:\s+мне)?(?:\s+кратко)?"
    r"(?:\s+(?:содержание|сводку|резюме|информацию))?"
)
_ARCHIVE_OWN = (
    r"(?:(?:мой|моя|мо[её]|мои|моего|моей|моих|свой|своя|сво[её]|свои|"
    r"своего|своей|своих)\s+)?"
)
_ARCHIVE_EXACT_SURFACE = re.compile(
    rf"{_ARCHIVE_LEAD}\s+{_ARCHIVE_OWN}{_ARCHIVE_SOURCE}\s+__file__",
    re.IGNORECASE,
)
_ARCHIVE_LATEST_SURFACE = re.compile(
    rf"{_ARCHIVE_LEAD}\s+{_ARCHIVE_OWN}(?:последний|последняя|последнее|последние|последних)\s+"
    rf"(?:[12]|один|одну|два|две)\s+{_ARCHIVE_SOURCE}",
    re.IGNORECASE,
)
_ARCHIVE_DATE_AFTER_SOURCE = re.compile(
    rf"{_ARCHIVE_LEAD}\s+{_ARCHIVE_OWN}{_ARCHIVE_SOURCE}\s+"
    r"(?:за\s+|(?:датированный|датированные|датированных)\s+)(?P<time>.+)",
    re.IGNORECASE,
)
_ARCHIVE_DATE_BEFORE_SOURCE = re.compile(
    rf"{_ARCHIVE_LEAD}\s+(?:полученный|полученные|полученных|присланный|присланные|"
    rf"присланных|загруженный|загруженные|загруженных|отправленный|отправленные|отправленных)\s+"
    rf"(?P<time>.+?)\s+{_ARCHIVE_OWN}{_ARCHIVE_SOURCE}",
    re.IGNORECASE,
)
_ARCHIVE_DAY_SURFACE = re.compile(r"(?:сегодня|вчера|позавчера)", re.IGNORECASE)
_BENIGN_FIELD_SUFFIX = re.compile(
    r"\s+и\s+(?:укажи|укажите)\s+поле\s+"
    r"(?:«\s*дата\s+документа\s*»|\"\s*дата\s+документа\s*\"|"
    r"'\s*дата\s+документа\s*'|“\s*дата\s+документа\s*”|"
    r"„\s*дата\s+документа\s*”|`\s*дата\s+документа\s*`)\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_QUOTE_GLYPH = re.compile(r"[«»\"'`“”„‟‘’‚‛]")
_UNOWNED_SURFACE_CHAR = re.compile(r"[^0-9A-Za-zА-ЯЁа-яё_ .,;:!?()\-]")


def _local_zone(settings: Any) -> Any:
    name = str(getattr(settings, "local_timezone", "") or "").strip()
    try:
        return ZoneInfo(name) if name else datetime.now().astimezone().tzinfo or UTC
    except (KeyError, ValueError):
        return datetime.now().astimezone().tzinfo or UTC


def _filename_matches(message: str) -> tuple[re.Match[str], ...]:
    return tuple(_QUOTED_FILENAME.finditer(message))


def _has_unbound_filename_token(message: str, matches: tuple[re.Match[str], ...]) -> bool:
    characters = list(message)
    for match in matches:
        characters[match.start() : match.end()] = " " * (match.end() - match.start())
    return _FILENAME_TOKEN.search("".join(characters)) is not None


def _person_queries(message: str, filename_matches: tuple[re.Match[str], ...]) -> tuple[str, ...]:
    # A filename such as ``@bob.txt`` is data, not an uploader selector.  Mask
    # every code-owned filename span before looking for explicit people.
    characters = list(message)
    for match in filename_matches:
        characters[match.start() : match.end()] = " " * (match.end() - match.start())
    surface = "".join(characters)
    return tuple(
        str(match.group("handle") or match.group("name") or "").strip().lstrip("@")
        for match in _EXPLICIT_PERSON.finditer(surface)
    )


def _archive_request_surface_is_closed(
    message: str,
    filename_matches: tuple[re.Match[str], ...],
) -> bool:
    """Admit only a small self-file speech grammar, never guessed ownership.

    The semantic planner may misclassify quoted, reported, negated or foreign-
    subject prose.  For this first historical canary the code-owned parser must
    therefore prove the complete authority surface instead of enumerating names
    and pronouns.  Unknown words make the turn legacy-owned.
    """

    surface = message
    for match in reversed(filename_matches):
        group = "quoted" if match.group("quoted") is not None else "plain"
        start, end = match.span(group)
        surface = surface[:start] + " __file__ " + surface[end:]
    surface = re.sub(r"[«\"`]\s*__file__\s*[»\"`]", " __file__ ", surface)
    surface = _BENIGN_FIELD_SUFFIX.sub("", surface)
    if _QUOTE_GLYPH.search(surface) or _UNOWNED_SURFACE_CHAR.search(surface):
        return False
    surface = " ".join(re.sub(r"[^0-9A-Za-zА-ЯЁа-яё_]+", " ", surface).casefold().split())
    if filename_matches:
        return _ARCHIVE_EXACT_SURFACE.fullmatch(surface) is not None
    if _LATEST.search(message):
        return _ARCHIVE_LATEST_SURFACE.fullmatch(surface) is not None
    date_match = _ARCHIVE_DATE_AFTER_SOURCE.fullmatch(surface) or _ARCHIVE_DATE_BEFORE_SOURCE.fullmatch(
        surface
    )
    if date_match is None:
        return False
    return _ARCHIVE_DAY_SURFACE.fullmatch(str(date_match.group("time") or "")) is not None


def _latest_count(value: str) -> int:
    return 1 if value.casefold() in {"1", "один", "одну"} else 2


def _has_temporal_surface(message: str, *, today: date) -> bool:
    return bool(
        lexical_time_window_kind(message, today=today) is not None
        or has_explicit_timezone(message)
        or has_invalid_clock_expression(message)
        or has_unsupported_time_granularity(message)
        or has_multiple_time_targets(message)
        or has_relational_clock_boundary(message)
        or has_mixed_time_direction(message)
    )


def _calendar_bounds(message: str, settings: Any) -> dict[str, str] | None:
    zone = _local_zone(settings)
    local_now = datetime.now(zone)
    visible = temporal_routing_text(message)
    document_date = bool(_DOCUMENT_DATE.search(visible))
    received_date = bool(_RECEIVED_DATE.search(visible))
    if (
        (document_date and received_date)
        or _NEGATED_DOCUMENT_DATE.search(visible)
        or has_explicit_timezone(message)
        or has_invalid_clock_expression(message)
        or has_unsupported_time_granularity(message)
        or has_multiple_time_targets(message)
        or has_relational_clock_boundary(message)
        or has_mixed_time_direction(message)
    ):
        return None
    kind = lexical_time_window_kind(message, today=local_now.date())
    if kind is None or kind == "single_hour":
        return None
    window = build_time_window(
        message,
        TimeIntent("past", kind),
        today=local_now.date(),
    )
    if window is None:
        return None
    try:
        first = date.fromisoformat(window.since[:10])
        last = date.fromisoformat(window.until[:10])
    except (TypeError, ValueError):
        return None
    if first > last or first > local_now.date() or last > local_now.date():
        return None
    if document_date:
        return {
            "document_since": first.isoformat(),
            "document_until": last.isoformat(),
        }
    local_start = datetime.combine(first, datetime_time.min, tzinfo=zone)
    local_limit = datetime.combine(last + timedelta(days=1), datetime_time.min, tzinfo=zone)
    local_end = min(local_limit - timedelta(microseconds=1), local_now)
    return {
        "received_since": local_start.astimezone(UTC).isoformat(),
        "received_until": local_end.astimezone(UTC).isoformat(),
    }


class V12ArchiveReadHandler(V12FileReadHandler):
    """Reuse the verified V12 synthesis/publication path over historical files."""

    route = RouteClass.ARCHIVE_READ

    def _select_raw_ids(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
        *,
        uploaded_by: str,
    ) -> tuple[tuple[str, ...], HistoricalFileSelectionToken | None]:
        filename_matches = _filename_matches(turn.message)
        if not _archive_request_surface_is_closed(turn.message, filename_matches):
            return (), None
        if len(filename_matches) > 1 or _has_unbound_filename_token(turn.message, filename_matches):
            return (), None
        filename = (
            str(filename_matches[0].group("quoted") or filename_matches[0].group("plain") or "").strip()
            if filename_matches
            else ""
        )
        latest_matches = tuple(_LATEST.finditer(turn.message))
        if len(latest_matches) > 1:
            return (), None
        today = datetime.now(_local_zone(self._settings)).date()
        has_time = _has_temporal_surface(turn.message, today=today)
        if filename and (latest_matches or has_time):
            return (), None
        if latest_matches and has_time:
            return (), None
        if filename:
            rows = self._storage.find_owned_files_by_filename(
                request.actor.user_id,
                uploaded_by,
                filename,
            )
            if len(rows) != 1:
                return (), None
            raw_ids: tuple[str, ...] = (str(rows[0].get("id") or ""),)
            if not archive_read_plan_supports_selection(plan, 1):
                return (), None
            return raw_ids, historical_file_selection_token(
                tenant_id=request.actor.user_id,
                uploaded_by=uploaded_by,
                kind="exact_filename",
                raw_ids=raw_ids,
                filename=filename,
            )

        latest = latest_matches[0] if latest_matches else None
        bounds = _calendar_bounds(turn.message, self._settings)
        if latest is None and bounds is None:
            return (), None
        requested = _latest_count(latest.group("count")) if latest is not None else _MAX_ARCHIVE_FILES
        selected = self._storage.select_owned_file_corpus(
            request.actor.user_id,
            uploaded_by,
            limit=_MAX_ARCHIVE_FILES + 1,
            offset=0,
            **(bounds or {}),
        )
        if not isinstance(selected, dict):
            return (), None
        rows = selected.get("items")
        if not isinstance(rows, list) or selected.get("unattributed") != 0 or selected.get("undated") != 0:
            return (), None
        total = selected.get("total")
        if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
            return (), None
        if latest is None:
            if total > _MAX_ARCHIVE_FILES or selected.get("page_complete") is not True:
                return (), None
            chosen_rows = rows
        else:
            chosen_rows = rows[:requested]
            if total < requested or len(chosen_rows) != requested:
                return (), None
        raw_ids = tuple(str(row.get("id") or "") for row in chosen_rows if isinstance(row, dict))
        if (
            len(raw_ids) != len(chosen_rows)
            or len(set(raw_ids)) != len(raw_ids)
            or not archive_read_plan_supports_selection(plan, len(raw_ids))
        ):
            return (), None
        if latest is not None:
            selector = historical_file_selection_token(
                tenant_id=request.actor.user_id,
                uploaded_by=uploaded_by,
                kind="latest",
                raw_ids=raw_ids,
                latest_count=requested,
            )
        else:
            assert bounds is not None
            selector = historical_file_selection_token(
                tenant_id=request.actor.user_id,
                uploaded_by=uploaded_by,
                kind="time_window",
                raw_ids=raw_ids,
                received_since=bounds.get("received_since"),
                received_until=bounds.get("received_until"),
                document_since=bounds.get("document_since"),
                document_until=bounds.get("document_until"),
            )
        return raw_ids, selector

    def _prepare_archive_sync(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
        absolute_deadline: float,
    ) -> _PreparedFileContext | None:
        if (
            request.user_id != request.actor.user_id
            or request.attachments
            or request.reply_to
            or request.replay_source_message_id
        ):
            return None
        filename_matches = _filename_matches(turn.message)
        # Cross-user archive reads retain the proven legacy owner until the V12
        # prepare phase has an audited, transaction-bound person authority.  A
        # named uploader therefore falls back before any historical body is read.
        if _person_queries(turn.message, filename_matches):
            return None
        uploaded_by = request.actor.own_id
        if time.monotonic() >= absolute_deadline:
            raise TimeoutError("archive evidence preparation deadline expired")
        raw_ids, selection = self._select_raw_ids(
            request,
            turn,
            plan,
            uploaded_by=uploaded_by,
        )
        if not raw_ids or selection is None:
            return None
        try:
            evidence = prepare_registered_file_evidence(
                self._storage,
                self._authorization,
                self._settings.files_dir,
                request.actor,
                uploaded_by=uploaded_by,
                selection=selection,
                max_bytes=self._settings.max_upload_bytes,
                absolute_deadline=absolute_deadline,
            )
        except (FileEvidenceUnavailable, TimeoutError):
            return None

        conversation_id = request.conversation_id
        if conversation_id is not None:
            conversation = self._storage.get_conversation(conversation_id, request.actor.own_id)
            if not isinstance(conversation, dict):
                return None
            interaction_mode = normalize_conversation_mode(str(conversation.get("mode") or "dialogue"))
        else:
            interaction_mode = normalize_conversation_mode(request.conversation_mode or "dialogue")
        return _PreparedFileContext(
            evidence=evidence,
            conversation_id=conversation_id,
            interaction_mode=interaction_mode,
        )

    async def _prepare_context(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
        absolute_deadline: float,
    ) -> _PreparedFileContext | None:
        return await asyncio.to_thread(
            self._prepare_archive_sync,
            request,
            turn,
            plan,
            absolute_deadline,
        )

    async def preparation_is_current(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
        preparation: ReadOnlyRoutePreparation,
    ) -> bool:
        prepared = self._prepared_matches(plan, preparation)
        if prepared is None or not await super().preparation_is_current(
            request,
            turn,
            plan,
            preparation,
        ):
            return False
        return await asyncio.to_thread(
            historical_file_selection_is_current,
            self._storage,
            prepared.evidence,
        )


__all__ = ["V12ArchiveReadHandler"]
