"""Capability-gated tool execution for the Friday agent runtime."""

from __future__ import annotations

import asyncio
import base64
import calendar
import hashlib
import inspect
import io
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import urllib.parse
import zipfile
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from friday.failures import safe_failure_text
from friday.file_delivery import (
    AuthorizedFileReadError,
    FileRecordUnavailable,
    read_authorized_file_in_transaction,
)
from friday.organs import local_now
from friday.oversight_scope import hierarchy_is_configured, may_oversee
from friday.people import resolve_person, unambiguous
from friday.permissions import (
    LEGACY_OWNER_USER_ID,
    ActorContext,
    AuthorizationError,
    AuthorizationService,
    current_actor,
)
from friday.private_fs import open_private_text_write
from friday.reminder_schedule import reminder_clock, reminder_clock_description, reminder_when_text
from friday.reports import SUPPORTED_KINDS, render, spec_from_payload
from friday.retrieval import _public_graph_context, best_snippet, is_relational_query
from friday.storage._core import iso_date
from friday.storage._oversight import ANALYSES
from friday.storage.models import (
    AuditEntry,
    EntityType,
    InboxStatus,
    RelationType,
    TaskStatus,
    new_id,
    normalize_known_at,
    utc_now,
)
from friday.tts import TTSUnavailable, synthesize_speech
from friday.web_surfer import (
    SEARCH_DOMAIN_LIST_MAX,
    SEARCH_FRESHNESS_VALUES,
    AllProvidersRefusedError,
    SearchFilterUnavailableError,
    normalize_search_domains,
    normalize_search_filters,
    normalize_search_language,
    normalize_search_region,
)
from friday.workers._blocking import run_blocking

if TYPE_CHECKING:
    from friday.config import FridaySettings
    from friday.executive import ExecutiveService
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph
    from friday.storage import FridayStorage
    from friday.web_surfer import WebSurfer

#: Ниже этого объёма страница знанием не становится — сохранять нечего.
_WEB_CAPTURE_MIN_CHARS = 200

#: Потолок собранного файла. Telegram принимает и больше, но отчёт на десятки
#: мегабайт — это не отчёт, а выгрузка, и её место не во вложении к реплике.
_MAX_GENERATED_FILE_BYTES = 12 * 1024 * 1024

#: Потолок ЗАПАКОВАННОГО архива. Отдельный от отчётного: архив по определению
#: везёт чужие файлы как есть, и двенадцати мегабайт на день загрузок мало.
#:
#: Telegram-бот принимает 50 МБ, но вложение едет к мосту внутри JSON в base64
#: (+33%), поэтому потолок ставится по каналу, а не по Telegram: 20 МБ на диске
#: — это 27 МБ строки в одном ответе.
#:
#: Замерено на живом архиве: 28 июля пришло 1605 файлов на 710 МБ. Потолок здесь
#: не формальность — он сработает на первом же массовом импорте, и потому обязан
#: сообщать о себе вслух.
_MAX_ARCHIVE_BYTES = 20 * 1024 * 1024

#: Сколько файлов кладётся в один архив. Не ради размера — ради времени сборки:
#: полторы тысячи файлов одного дня человек в чате не ждёт.
_MAX_ARCHIVE_FILES = 300

LOGGER = logging.getLogger(__name__)
Handler = Callable[..., Awaitable[dict[str, Any]]]


def _storage_read_snapshot(storage: Any, operation: Callable[[], Any]) -> Any:
    """Run related SELECTs against one deferred WAL snapshot in one thread."""

    connection = storage.conn
    owns_snapshot = not connection.in_transaction
    if owns_snapshot:
        connection.execute("BEGIN")
    try:
        return operation()
    finally:
        if owns_snapshot and connection.in_transaction:
            connection.rollback()


# Execution scope is code-owned context, not a model/tool argument.  A mission
# receives only the bounded gather surface below; every other tool remains a
# dialogue-only capability even when the mission actor is otherwise authorized
# to use it.  The kernel checks this immediately before dispatch as well as when
# definitions are selected, because the list shown to a model is not a security
# boundary.
EXECUTION_SCOPES = frozenset({"dialogue", "mission"})
MISSION_EXECUTION_TOOLS = frozenset(
    {
        "memory_search",
        "message_search",
        "entity_lookup",
        "kg_stats",
        "inbox_list",
        "web_search",
        "web_fetch",
        "web_research",
    }
)


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """Best-effort hard stop for a timed-out executor and all descendants.

    The GROUP is killed whether or not the direct child is still alive. It used
    to return early on `returncode is not None`, which reads as «nothing to kill»
    and is exactly wrong: untrusted code that spawns a helper and exits leaves a
    live process group behind a dead leader. Reproduced against the repository's
    own timeout test with one change — the child exits instead of sleeping — and
    its `assert not marker.exists()` failed: the orphan wrote its file two
    seconds AFTER the tool had reported «timed out». The process group id is the
    dead leader's pid and stays valid while any member lives, so `killpg` still
    reaches them; `ProcessLookupError` covers the empty case.
    """

    if os.name == "posix":
        # Code executors are started in a new session, so their PID is also the
        # process-group ID inherited by descendants.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    elif os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except (FileNotFoundError, OSError):
            with suppress(ProcessLookupError):
                process.kill()
    else:
        with suppress(ProcessLookupError):
            process.kill()
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except (TimeoutError, ProcessLookupError):
        with suppress(ProcessLookupError):
            process.kill()


async def _collect_bounded_process_output(
    process: asyncio.subprocess.Process,
    max_bytes: int,
) -> tuple[bytes, bytes, bool, bool]:
    """Drain child pipes without ever retaining more than ``max_bytes`` total.

    Once the combined stdout/stderr budget is exceeded, the whole process tree
    is terminated. Truncating only after ``communicate()`` would allow an
    untrusted script to consume arbitrary parent-process memory first.
    """
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("Code runner pipes were not created")
    limit = max(1, int(max_bytes))
    stdout = bytearray()
    stderr = bytearray()
    captured = 0
    limit_exceeded = asyncio.Event()

    async def read_stream(stream: asyncio.StreamReader, destination: bytearray) -> None:
        nonlocal captured
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                return
            remaining = max(0, limit - captured)
            if remaining:
                kept = chunk[:remaining]
                destination.extend(kept)
                captured += len(kept)
            if len(chunk) > remaining:
                limit_exceeded.set()

    readers = [
        asyncio.create_task(read_stream(process.stdout, stdout)),
        asyncio.create_task(read_stream(process.stderr, stderr)),
    ]
    process_waiter = asyncio.create_task(process.wait())
    limit_waiter = asyncio.create_task(limit_exceeded.wait())
    terminated_for_limit = False
    try:
        done, _ = await asyncio.wait(
            {process_waiter, limit_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        # No `returncode is None` guard here either: the flood may be coming from
        # a descendant while the leader has already exited, which is precisely
        # when the group still needs killing.
        if limit_waiter in done and limit_exceeded.is_set():
            terminated_for_limit = True
            await _terminate_process_tree(process)
        await process_waiter
        await asyncio.gather(*readers)
    except asyncio.CancelledError:
        await _terminate_process_tree(process)
        for task in readers:
            task.cancel()
        process_waiter.cancel()
        limit_waiter.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.gather(*readers, process_waiter, limit_waiter)
        raise
    finally:
        limit_waiter.cancel()
        with suppress(asyncio.CancelledError):
            await limit_waiter
    return bytes(stdout), bytes(stderr), limit_exceeded.is_set(), terminated_for_limit


def _window_bound(value: str | None, *, edge: str) -> tuple[str | None, str | None]:
    """Граница периода в `ГГГГ-ММ-ДД`, либо причина, по которой её не понять.

    Границы приходят СТРОКОЙ ОТ МОДЕЛИ и уходили прямо в SQL как операнды сравнения
    строк. Проверено запуском на трёх документах за март 2023: окно
    «01.01.2025..31.01.2025» возвращало все три — посимвольно `'2023-03-10' >=
    '01.01.2025'` истинно, — то есть фильтр молча снимался, и мартовские документы
    выдавались как январские. Форма дд.мм.гггг здесь не экзотика: в самом архиве
    владельца 2537 значений дат из 3180 записаны именно так, и модель перепишет её
    из документа. Зеркальный отказ: «2023-03» давало ноль там, где документов три.

    HTTP-маршруты эту форму проверяют шаблоном, и на это есть отдельный тест с
    обоснованием «опечатка не должна тихо снимать фильтр». Путь инструмента — то
    есть Telegram, главный вход владельца, — этой проверки не имел вовсе.

    Неполная дата достраивается к своему краю: как начало — к первому дню периода,
    как конец — к последнему. Только так «с 2023-03 по 2023-03» означает весь март,
    а не один нулевой день. Непонятое НЕ становится «без фильтра»: возвращается
    причина, и вызывающий обязан сказать о ней вслух.
    """
    text = " ".join(str(value or "").split())
    if not text:
        return None, None
    exact = iso_date(text)
    if exact:
        return exact, None
    year_month = re.fullmatch(r"(\d{4})[-./](\d{1,2})", text) or re.fullmatch(r"(\d{1,2})[-./](\d{4})", text)
    if year_month:
        first, second = year_month.groups()
        year, month = (int(first), int(second)) if len(first) == 4 else (int(second), int(first))
        if 1900 <= year <= 2200 and 1 <= month <= 12:
            if edge == "since":
                return f"{year:04d}-{month:02d}-01", None
            last = calendar.monthrange(year, month)[1]
            return f"{year:04d}-{month:02d}-{last:02d}", None
    if re.fullmatch(r"\d{4}", text) and 1900 <= int(text) <= 2200:
        return (f"{text}-01-01" if edge == "since" else f"{text}-12-31"), None
    return None, text


#: «26 июля в 15 часов», «2026-07-26 15:00», «26.07.2026 15:30».
#: Человек назвал день недели, а не дату. Для напоминания это значит ближайший
#: БУДУЩИЙ такой день: разбор времени писался для вопросов о прошлом и берёт
#: прошедший.
_NAMES_A_WEEKDAY = re.compile(
    r"\bпонедельник\w*|\bвторник\w*|\bсред[уые]\b|\bчетверг\w*|\bпятниц\w*|"
    r"\bсуббот\w*|\bвоскресен\w*",
    re.IGNORECASE,
)
_MOMENT_RE = re.compile(
    r"^\s*(?P<date>\S+(?:\s+\S+){0,2}?)"
    r"(?:\s*[, ]\s*(?:в\s+)?(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2}))?\s*(?:час\w*|ч|h)?)?\s*$",
    re.IGNORECASE,
)
_NORMALIZED_LOCAL_TIMESTAMP = re.compile(
    r"^(?P<day>\d{4}-\d{2}-\d{2})T(?P<hour>[01]\d|2[0-3]):"
    r"(?P<minute>[0-5]\d):(?P<second>[0-5]\d)$"
)


def _normalized_local_timestamp(value: str) -> str | None:
    text = str(value or "").strip()
    if not _NORMALIZED_LOCAL_TIMESTAMP.fullmatch(text):
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.isoformat(timespec="seconds") if parsed.tzinfo is None else None


#: Месяцы прописью — по префиксу, чтобы падеж не имел значения («июля», «июле»).
_MONTHS_RU = {
    "янв": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "ма": 5,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сен": 9,
    "окт": 10,
    "ноя": 11,
    "дек": 12,
}
#: «26 июля», «3-го августа 2026» — день с месяцем, с порядковым окончанием или без.
_DAY_MONTH_RE = re.compile(r"^(\d{1,2})(?:\s*-?\s*(?:го|е|ое))?\s+([а-яё]+)(?:\s+(\d{4}))?$", re.IGNORECASE)
_DAYS_AGO_RE = re.compile(r"^(\d{1,3})\s+(?:дн\w*|сут\w*)\s+назад$", re.IGNORECASE)
#: «29го», «29-го», «3-е» — порядковый день без месяца. Так пишут в переписке,
#: и на демо это одна из сценарных строк.
_ORDINAL_DAY_RE = re.compile(r"^(\d{1,2})\s*-?\s*(?:го|е|ое|ым)$", re.IGNORECASE)
_RELATIVE_DAYS = {"сегодня": 0, "вчера": 1, "позавчера": 2}
#: Дни недели: «в понедельник» значит ближайший ПРОШЕДШИЙ понедельник — о будущем
#: архив ничего сказать не может, там ещё ничего не произошло.
_WEEKDAYS_RU = (
    ("понедельник", 0),
    ("вторник", 1),
    ("сред", 2),
    ("четверг", 3),
    ("пятниц", 4),
    ("суббот", 5),
    ("воскресен", 6),
)


def _spoken_day(text: str, *, today: date) -> str | None:
    """«26 июля», «31 июля 2026», «вчера», «три дня назад» → `ГГГГ-ММ-ДД`.

    Так люди и называют дни; требовать от человека ISO значит требовать от него
    работы, которую машина делает лучше. Год без указания — текущий: спрашивают
    почти всегда про этот.

    Угадывать «весной» или «прошлым летом» здесь по-прежнему нельзя — это тот же
    класс ошибки, что придумать дату документа.
    """
    lowered = " ".join(str(text or "").split()).casefold()
    if not lowered:
        return None
    if lowered in _RELATIVE_DAYS:
        return (today - timedelta(days=_RELATIVE_DAYS[lowered])).isoformat()
    ordinal = _ORDINAL_DAY_RE.match(lowered)
    if ordinal:
        # Число без месяца означает ближайшее ПРОШЕДШЕЕ такое число: о будущем
        # архив ничего сказать не может, там ещё ничего не произошло.
        day_number = int(ordinal.group(1))
        if not 1 <= day_number <= 31:
            return None
        year, month = today.year, today.month
        for _ in range(14):  # хватает, чтобы миновать февраль и короткие месяцы
            try:
                candidate = date(year, month, day_number)
            except ValueError:
                candidate = None
            if candidate is not None and candidate <= today:
                return candidate.isoformat()
            month -= 1
            if month == 0:
                month, year = 12, year - 1
        return None
    for prefix, weekday in _WEEKDAYS_RU:
        if lowered.startswith(prefix):
            # Сегодняшний день недели засчитывается как сегодня, иначе «в субботу»,
            # сказанное в субботу, уводило бы на неделю назад.
            back = (today.weekday() - weekday) % 7
            return (today - timedelta(days=back)).isoformat()
    ago = _DAYS_AGO_RE.match(lowered)
    if ago:
        return (today - timedelta(days=int(ago.group(1)))).isoformat()
    spoken = _DAY_MONTH_RE.match(lowered)
    if not spoken:
        return None
    day, month_word, year_text = spoken.groups()
    month = next((number for prefix, number in _MONTHS_RU.items() if month_word.startswith(prefix)), 0)
    if not month:
        return None
    year = int(year_text) if year_text else today.year
    try:
        return date(year, month, int(day)).isoformat()
    except ValueError:
        return None


#: Час в словах человека: «в 15:00», «в 15 часов», «в 10 утра».
_CLOCK_IN_TEXT = re.compile(
    r"(?:^|\D)(?:в|к|на)?\s*(?P<hour>[01]?\d|2[0-3])[:.](?P<minute>[0-5]\d)"
    r"|(?:^|\D)(?:в|к)\s*(?P<hour2>[01]?\d|2[0-3])\s*час"
    r"|(?:^|\D)(?:в|к)\s*(?P<hour3>0?[1-9]|1[0-2])\s*"
    r"(?P<period>утра|дня|вечера|ночи)\b",
    re.IGNORECASE,
)


def _clock_from_text(value: str) -> str:
    """Parse the wall-clock forms advertised by the reminder tool schema."""

    match = _CLOCK_IN_TEXT.search(str(value or ""))
    if not match:
        return ""
    hour = int(match.group("hour") or match.group("hour2") or match.group("hour3") or 0)
    minute = int(match.group("minute") or 0)
    period = str(match.group("period") or "").casefold()
    if period in {"дня", "вечера"} and hour < 12:
        hour += 12
    elif period == "утра" and hour == 12:
        hour = 0
    elif period == "ночи":
        if hour == 12:
            hour = 0
        elif hour >= 6:
            hour += 12
    return f"{hour:02d}:{minute:02d}" if 0 <= hour <= 23 and 0 <= minute <= 59 else ""


#: Дни недели по-русски → номер в неделе, как их считает `date.weekday()`.
_WEEKDAY_NUMBERS = (
    ("понедельник", 0),
    ("вторник", 1),
    ("сред", 2),
    ("четверг", 3),
    ("пятниц", 4),
    ("суббот", 5),
    ("воскресен", 6),
)


def _future_day(text: str, *, today: date) -> str | None:
    """«завтра», «в понедельник», «через неделю» → `ГГГГ-ММ-ДД` в БУДУЩЕМ.

    Разбор времени в этом модуле писался для вопросов о прошлом: «вчера»,
    «три дня назад», число без месяца — ближайшее ПРОШЕДШЕЕ. Для напоминания всё
    ровно наоборот, и на живом прогоне это стоило дефекта: «не дай забыть в
    понедельник позвонить» поставило событие на прошлую неделю, то есть не
    сработало бы никогда. Отдельная функция, а не флаг в общей: два разных
    вопроса — «когда это было» и «когда напомнить» — и путать их нельзя.
    """
    lowered = " ".join(str(text or "").split()).casefold()
    if not lowered:
        return None
    if "послезавтра" in lowered:
        return (today + timedelta(days=2)).isoformat()
    if "завтра" in lowered:
        return (today + timedelta(days=1)).isoformat()
    if "сегодня" in lowered or "вечером" in lowered or "к вечеру" in lowered:
        return today.isoformat()
    through = re.search(
        r"через\s+(\d+|неделю|день|дня|дней|месяц)\s*(день|дня|дней|недел\w*|месяц\w*)?", lowered
    )
    if through:
        amount_text, unit_text = through.group(1), through.group(2) or ""
        amount = 1 if not amount_text.isdigit() else int(amount_text)
        unit = unit_text or amount_text
        if unit.startswith("недел"):
            return (today + timedelta(weeks=max(1, amount))).isoformat()
        if unit.startswith("месяц"):
            return (today + timedelta(days=30 * max(1, amount))).isoformat()
        return (today + timedelta(days=max(1, amount))).isoformat()
    for name, number in _WEEKDAY_NUMBERS:
        if name in lowered:
            ahead = (number - today.weekday()) % 7
            return (today + timedelta(days=ahead or 7)).isoformat()
    # A calendar date without a year denotes its next occurrence for a future
    # effect.  Falling through to the history-oriented parser below anchored it
    # to the current year, so ``3 августа`` said on 8 August was rejected as a
    # past date even though this exact yearless form is advertised by the tool
    # schema.  An explicitly named year remains authoritative and is allowed to
    # reach ``_remind``'s ordinary past-date refusal.
    moment = _MOMENT_RE.match(lowered)
    spoken = _DAY_MONTH_RE.match(moment.group("date") if moment else lowered)
    if spoken:
        day_text, month_word, year_text = spoken.groups()
        month = next(
            (number for prefix, number in _MONTHS_RU.items() if month_word.startswith(prefix)),
            0,
        )
        if not month:
            return None
        if year_text:
            try:
                return date(int(year_text), month, int(day_text)).isoformat()
            except ValueError:
                return None
        for year in range(today.year, today.year + 9):
            try:
                candidate = date(year, month, int(day_text))
            except ValueError:
                continue
            if candidate >= today:
                return candidate.isoformat()
    return None


def _moment_bounds(value: str, *, edge: str, widen: bool = False) -> tuple[str | None, str | None]:
    """Граница промежутка с точностью до часа, а не только до дня.

    `_window_bound` понимает дни — этого хватает хронике архива, но не вопросу
    «что было 26 июля в 15 часов»: там день целиком означал бы ответ не о том,
    о чём спросили. Здесь к разобранному дню добавляется время, если оно названо:
    для начала — начало часа, для конца — его последняя секунда.

    Час без даты не принимается: «в 15 часов» без дня — это не момент.

    `widen` расширяет названную минуту до конца её часа. Нужен там, где конец
    промежутка не назван вовсе: «что было в 15:30» — это вопрос про пятнадцатый
    час, а не про одну минуту.
    """
    text = " ".join(str(value or "").split())
    if not text:
        return None, None
    match = _MOMENT_RE.match(text)
    if not match:
        return _window_bound(text, edge=edge)
    spoken_text = match.group("date")
    day = _spoken_day(spoken_text, today=datetime.now().date())
    if day is None:
        day, bad = _window_bound(spoken_text, edge=edge)
        if day is None:
            return None, bad or text
    hour_text = match.group("hour")
    if hour_text is None:
        # День целиком: с его начала до последней секунды.
        return (f"{day}T00:00:00" if edge == "since" else f"{day}T23:59:59"), None
    hour = int(hour_text)
    if not 0 <= hour <= 23:
        return None, text
    minute_text = match.group("minute")
    if minute_text is None:
        # Час целиком — так это и слышится: «в 15 часов» значит с 15:00 до 15:59.
        return (f"{day}T{hour:02d}:00:00" if edge == "since" else f"{day}T{hour:02d}:59:59"), None
    minute = int(minute_text)
    if not 0 <= minute <= 59:
        return None, text
    if edge == "since":
        return f"{day}T{hour:02d}:{minute:02d}:00", None
    if widen:
        return f"{day}T{hour:02d}:59:59", None
    return f"{day}T{hour:02d}:{minute:02d}:59", None


def _count_user_tasks() -> int:
    """How many TASKS the real UID already owns — what RLIMIT_NPROC actually counts.

    Threads, not processes: Linux checks this limit in `copy_process`, so every thread
    counts. Measured here, 114 processes were 200-odd tasks, and a ceiling computed from
    the process count refused the executor's very first fork.

    Counted in the PARENT on purpose. `preexec_fn` runs between fork and exec, where
    doing this much work is a bad idea; the number only has to be close enough to leave
    the executor headroom, and it is captured before the child exists.
    """
    uid = os.getuid()
    count = 0
    try:
        entries = os.listdir("/proc")
    except OSError:
        return 1 << 20  # not Linux, or /proc not mounted: stay out of the way
    for entry in entries:
        if not entry.isdigit():
            continue
        with suppress(OSError):
            if os.stat(f"/proc/{entry}").st_uid != uid:
                continue
            count += len(os.listdir(f"/proc/{entry}/task"))
    return max(count, 1)


def _merge_needs_a_person(arguments: dict[str, Any]) -> bool:
    """Слияние — да, отказ — нет.

    `reject` помечает пару «не дубликат»: пара уходит из очереди, но НИ ОДИН узел
    не меняется, и решение переигрывается новым проходом дедупа. `accept` переносит
    связи и оставляет от двух сущностей одну — ошибка здесь означает двух разных
    людей под одним узлом.
    """
    return str(arguments.get("decision") or "").strip().casefold() == "accept"


def _conflict_needs_a_person(arguments: dict[str, Any]) -> bool:
    """`dismiss` ничего не трогает; `keep_a`/`keep_b` объявляют знание устаревшим."""
    return str(arguments.get("decision") or "").strip().casefold() in {"keep_a", "keep_b"}


def _merge_postcondition(storage, user_id: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    """Слияние действительно случилось — прочитано из хранилища, а не из ответа.

    Спека v3 §5: успешный вызов инструмента не доказывает успех задачи. Обработчик
    возвращает то, что сам про себя думает; здесь проверяется ФАКТ — и на всякий
    случай оба его следа сразу, потому что частично применённое слияние (кандидат
    закрыт, а узел не помечен) выглядит как успех ровно до тех пор, пока кто-нибудь
    не спросит про исходную сущность.
    """
    candidate = storage.get_resolution_candidate(str(arguments.get("candidate_id") or ""), user_id)
    if not candidate:
        return False, "кандидат слияния исчез"
    status = str(candidate.get("status") or "")
    if status != "merged":
        return False, f"кандидат остался в статусе {status!r}"
    source = storage.get_entity(str(candidate.get("entity_a_id") or ""), user_id)
    target = storage.get_entity(str(candidate.get("entity_b_id") or ""), user_id)
    merged_marks = [
        str((row or {}).get("merged_into_id") or "") for row in (source, target) if row is not None
    ]
    if not any(merged_marks):
        return False, "ни одна из сущностей не помечена слитой"
    return True, ""


def _conflict_postcondition(storage, user_id: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    """Вердикт по противоречию: конфликт закрыт И проигравший помечен устаревшим."""
    conflict = storage.get_knowledge_conflict(user_id, str(arguments.get("conflict_id") or ""))
    if not conflict:
        return False, "конфликт исчез"
    status = str(conflict.get("status") or "")
    if status == "suggested":
        return False, "конфликт остался нерешённым"
    decision = str(arguments.get("decision") or "").strip().casefold()
    loser_id = str(conflict.get("knowledge_b_id" if decision == "keep_a" else "knowledge_a_id") or "")
    loser = storage.get_knowledge_object(loser_id, user_id)
    if loser and str(loser.get("lifecycle_stage") or "") != "deprecated":
        return False, "проигравшая запись не помечена устаревшей"
    return True, ""


# Что должно стать правдой ПОСЛЕ действия, проверенное чтением хранилища заново.
# Инструмента здесь может не быть: у `code_run` постусловия не существует — его
# результат и есть вывод программы, проверять в базе нечего, и выдумывать проверку
# ради симметрии значило бы проверять пустоту.
def _compensation_postcondition(storage, user_id: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    """Шаг, который человек закрыл, действительно перестал быть неизвестным.

    Проверять здесь есть что, и это не формальность: успешный возврат обработчика
    означает лишь «UPDATE выполнился», а нужен ответ на другой вопрос — остался ли
    шаг висеть. Именно вечно висящий `uncertain` и был исходным дефектом, поэтому
    сверка идёт по статусу, а не по факту записи.
    """
    task_id = str(arguments.get("task_id") or "")
    mission_id = str(arguments.get("mission_id") or "")
    tasks = storage.get_mission_tasks(mission_id, user_id)
    task = next((item for item in tasks if str(item.get("id")) == task_id), None)
    if task is None:
        return False, "шаг миссии исчез"
    status = str(task.get("status") or "")
    if status != "compensated":
        return False, f"шаг остался в состоянии {status!r}"
    return True, ""


# Что должно стать правдой ПОСЛЕ действия, проверенное чтением хранилища заново.
# Инструмента здесь может не быть: у `code_run` постусловия не существует — его
# результат и есть вывод программы, проверять в базе нечего, и выдумывать проверку
# ради симметрии значило бы проверять пустоту.
POSTCONDITIONS: dict[str, Callable[[Any, str, dict[str, Any]], tuple[bool, str]]] = {
    "entity_merge_decide": _merge_postcondition,
    "conflict_decide": _conflict_postcondition,
    "mission_compensation": _compensation_postcondition,
}


# Какие вызовы модели не исполняются без человека. Ключ — имя инструмента,
# значение — предикат по аргументам, потому что риск живёт в аргументах, а не в
# инструменте: `entity_merge_decide` с `decision=reject` безопасен, с `accept` —
# нет. Спека v3 §5: модель предлагает, служба авторизует и исполняет.
HIGH_RISK_TOOLS: dict[str, Callable[[dict[str, Any]], bool]] = {
    "entity_merge_decide": _merge_needs_a_person,
    "conflict_decide": _conflict_needs_a_person,
    "code_run": lambda _arguments: True,
    # Компенсация закрывает шаг, у которого исход НЕИЗВЕСТЕН, и решить это может
    # только человек: он один способен посмотреть на мир и сказать, случился ли
    # побочный эффект. Аргументы тут ни при чём — опасен сам вопрос.
    "mission_compensation": lambda _arguments: True,
}


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    security_id: str
    # Класс риска — ОБЯЗАТЕЛЬНОЕ поле, без умолчания, и это главное в нём.
    # Спека v3 §5 требует различать наблюдение и мутацию; список опасных
    # инструментов, живущий отдельно от них самих, — это fail-open: новый
    # инструмент, меняющий данные, по умолчанию не попадает под гейт, и никто
    # об этом не узнает, пока модель что-нибудь не сделает. Умолчания нет
    # намеренно: забыть класс невозможно, программа не соберётся.
    #
    #   observe — только читает;
    #   mutate  — меняет данные, но обратимо и в пределах своего арендатора;
    #   high    — необратимо, каноническое или исполняющее; требует человека,
    #             если предикат из HIGH_RISK_TOOLS подтверждает риск аргументов.
    risk: str
    handler: Handler | None = None
    allowed_execution_scopes: frozenset[str] = frozenset({"dialogue"})

    def __post_init__(self) -> None:
        scopes = frozenset(self.allowed_execution_scopes)
        if not scopes or not scopes <= EXECUTION_SCOPES:
            raise ValueError(
                f"unknown or empty execution scope for tool {self.name!r}: "
                f"{sorted(str(scope) for scope in scopes)!r}"
            )
        self.allowed_execution_scopes = scopes

    def to_openai(self, *, brief: bool = False) -> dict[str, Any]:
        """Описание для модели. `brief` — короткая форма, одна фраза.

        Окно модели 32 768 токенов, а полные описания всех инструментов — 4 650
        токенов в КАЖДОМ вызове, и вызовов на один ход несколько. При этом
        подробности нужны не всем: в разговоре о погоде незачем объяснять, как
        разбирать очередь слияний сущностей.

        Инструмент остаётся ДОСТУПНЫМ в любом случае — сокращается только
        описание. Это важно: набор, урезанный по догадке о теме, отнимал бы
        способности («напомни завтра» посреди разговора о курсе валют), а
        короткая строка лишь делает вызов менее вероятным, но не невозможным.
        """
        description = self.description
        if brief:
            # Первая фраза несёт назначение; остальное — оговорки и примеры,
            # которые нужны, когда инструмент реально в деле.
            head = description.split(". ", 1)[0].strip()
            description = (head + ".") if head and not head.endswith(".") else (head or description)
            # Девяносто знаков — примерно строка. Хватает назвать назначение
            # («Поставить напоминание», «Поиск по своему архиву»), и этого
            # достаточно, чтобы модель узнала инструмент, если он вдруг нужен;
            # за подробностями она его просто вызовет и увидит отказ с
            # объяснением. Замерено: полная форма 4 650 токенов, короткая для
            # неуместных — около 2 900.
            if len(description) > 90:
                description = description[:87].rstrip(" ,;:—-") + "…"
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolResult:
    tool_name: str
    success: bool
    data: Any = None
    error: str = ""
    truncated: bool = False
    # Out-of-band artifact (e.g. a synthesized voice clip) a tool produced this
    # call. Deliberately excluded from `to_dict()`/`to_llm_message()`: models are
    # untrusted reasoning components, not a transport for binary payloads, and a
    # base64 audio blob would blow the LLM context budget for no benefit. Callers
    # that need it (the agentic loop, for delivery to the user) read this field
    # directly instead.
    attachment: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"tool": self.tool_name, "success": self.success}
        if self.data is not None:
            encoded = self.data if isinstance(self.data, str) else json.dumps(self.data, ensure_ascii=False)
            if len(encoded) > 8_000:
                encoded = encoded[:7_900] + "\n… (truncated)"
                self.truncated = True
            result["result"] = encoded
        if self.error:
            result["error"] = self.error
        return result

    def to_llm_message(self) -> str:
        if not self.success:
            return f"Ошибка инструмента {self.tool_name}: {self.error}"
        if self.tool_name == "web_research" and isinstance(self.data, dict):
            encoded, compacted = _web_research_for_llm(self.data)
            self.truncated = self.truncated or compacted
        else:
            encoded = (
                self.data
                if isinstance(self.data, str)
                else json.dumps(self.data, ensure_ascii=False, indent=2)
            )
        if len(encoded) > 12_000:
            encoded = encoded[:11_900] + "\n… (truncated)"
            self.truncated = True
        return f"Результат {self.tool_name}:\n{encoded}"


_LLM_TOOL_PAYLOAD_MAX_CHARS = 11_900
#: Сколько меток показывается модели. Список отсортирован по частоте, поэтому
#: сорок первых отвечают на вопрос «какие у меня темы»; общее число возвращается
#: отдельным полем, чтобы «показаны не все» не выглядело как «их столько».
_TAGS_SHOWN_TO_LLM = 40


def _person_answer_for_llm(answer: dict[str, Any], *, zone: Any) -> dict[str, Any]:
    """Ответ о человеке в том виде, в каком его можно ПЕРЕСКАЗАТЬ вслух.

    Найдено владельцем 2026-08-03, когда инструмент уже вызывался и данные
    приходили. Он получил в чат: «в личной базе знаний записей от пользователя
    Пегас не найдено. Однако в данных активности видно, что у пользователя с
    display_name Пегас есть 92 сообщения».

    Причина в форме. Модели уходил служебный JSON — `display_name`, `username`,
    `confidence`, `method`, `matched_on`, `user_id`, `conversation_id`, — и она
    честно пересказывала эти слова человеку. А поля `knowledge_objects: 0` и
    `arrivals: 0` (это про ЗАГРУЖЕННЫЕ ФАЙЛЫ, которых у переписывающегося
    человека нет) читались как «в базе ничего нет», и ответ начинался с
    оправдания вместо ответа.

    Здесь остаётся то, что человек и так знает: имя, сколько сообщений, когда
    писал и что именно. Время — местное, потому что спрашивают про свой день.
    """
    resolved = answer.get("resolved")
    resolved = resolved if isinstance(resolved, dict) else {}
    summary = answer.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    name = str(resolved.get("display_name") or resolved.get("username") or "").strip()

    def local(stamp: str) -> str:
        try:
            return datetime.fromisoformat(str(stamp)).astimezone(zone).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            return str(stamp or "")

    messages = []
    for item in answer.get("messages") or []:
        row = item if isinstance(item, dict) else {}
        messages.append({"когда": local(str(row.get("at") or "")), "текст": row.get("text", "")})

    trimmed: dict[str, Any] = {
        "человек": name,
        "сообщений всего": summary.get("messages", 0),
        "что писал": messages,
        # Уровень доступа остаётся в ответе: он говорит модели, почему текстов
        # нет, — иначе она объявит, что человек ничего не писал, хотя сообщения
        # есть, просто их содержание закрыто.
        "доступ": "полный" if str(answer.get("content") or "") == "full" else "без содержания",
    }
    # Как именно опознан человек. Неточное совпадение имени — это риск ответить
    # ПРО ДРУГОГО, и модель обязана знать, что уверенности нет.
    if resolved and str(resolved.get("method") or "") not in {"exact", ""}:
        trimmed["опознан приблизительно"] = (
            f"по написанию «{resolved.get('matched_on') or ''}» — если это не тот человек, уточни"
        )
    if answer.get("denied"):
        return {"человек": name, "отказано": True, "причина": answer.get("reason")}
    # Файлы упоминаются ТОЛЬКО когда они есть: ноль загрузок у человека, который
    # просто переписывается, — это норма, а не отсутствие данных.
    files = answer.get("items") or []
    if files:
        trimmed["присылал файлов"] = summary.get("arrivals", len(files))
        trimmed["файлы"] = [
            {"когда": local(str((row or {}).get("at") or "")), "что": (row or {}).get("title")}
            for row in files[:10]
        ]
    if answer.get("analysis"):
        trimmed["разбор"] = answer["analysis"]
    if not messages and not files:
        # «Ничего не нашлось» и «ничего не было» — РАЗНЫЕ утверждения, и второе
        # здесь неправда.
        #
        # Замерено на живой базе 2026-08-04: все 3295 документов лежат под ОДНИМ
        # идентификатором (общий архив), а признака автора у них нет вовсе —
        # система не записывает, кто загрузил. Значит вопрос «что Иван присылал»
        # сегодня неразрешим в принципе: поиск по человеку даёт ноль всегда, а не
        # потому, что человек ничего не присылал.
        #
        # Прежняя формулировка превращала это в уверенное отрицание про живого
        # человека, на котором строится кадровое суждение. Теперь ход честно
        # называет, ЧЕГО именно нет: переписки за период не нашлось, а по
        # материалам ответа нет вообще.
        trimmed["переписки за период не нашлось"] = True
        # Ключа НЕТ — значит никто не считал, и это не то же самое, что «посчитали
        # и вышел ноль». Умолчание здесь осторожное: уверенное отрицание про живого
        # человека, на котором строится кадровое суждение, дороже лишней оговорки.
        raw_nameless = answer.get("arrivals_without_an_author")
        try:
            nameless = 1 if raw_nameless is None else int(raw_nameless)
        except (TypeError, ValueError):
            nameless = 1
        if nameless > 0:
            # Есть документы, у которых автор неизвестен, — значит утверждать «он
            # ничего не присылал» нельзя: его загрузки могли быть среди них.
            # Факт в прошедшем времени, без указаний себе.
            #
            # Здесь стояло «Не отвечай… Скажи…» — служебная строка ВНУТРИ данных
            # инструмента. Модель не отличает данные от инструкции: такая же
            # уехала владельцу целиком, вместе со словами «Скажи это человеку
            # прямо и не обещай файл». Класс чинился дважды за двое суток.
            #
            # Само содержание при этом верное и его надо сохранить: неизвестность
            # автора не даёт утверждать, что человек ничего не присылал.
            trimmed["про загруженные материалы"] = (
                f"за этот период в архиве {nameless} материалов без отметки о том, кто их "
                "загрузил (признак появился позже них); его загрузки могли быть среди них, "
                "и по загрузкам данных нет."
            )
        else:
            # Все загрузки окна подписаны, и его среди них нет. Здесь отрицание —
            # уже не домысел, а факт, и прятать его за оговоркой значит отвечать
            # «не знаю» на вопрос, ответ на который есть.
            trimmed["про загруженные материалы"] = (
                "за этот период все загрузки помечены авторами, и его среди них нет — "
                "материалов от него не поступало."
            )
    return trimmed


def _timeline_event_for_llm(event: dict[str, Any]) -> dict[str, Any]:
    """Одно событие ленты без того, что модели не нужно.

    Замерено на живом архиве 2026-08-02: `what_happened` отдавал 11 939 знаков —
    предел инструмента, — и обрезался посреди структуры, теряя хвост дня молча.
    Внутри при этом лежало лишнее: время в двух форматах (UTC и местное),
    внутренний `conversation_id`, а заголовок разговора у первого сообщения
    ДОСЛОВНО повторял его же текст.

    Остаётся местное время (о нём и спрашивают), роль, вид и текст.
    """
    text = str(event.get("text") or "")
    conversation = str(event.get("conversation") or "")
    trimmed: dict[str, Any] = {
        "kind": event.get("kind"),
        "at": event.get("at_local") or event.get("at"),
        # 200 знаков: этого хватает узнать реплику, а сорок реплик по 400 снова
        # упирались в предел инструмента и обрезались посреди дня.
        "text": text[:200],
    }
    if event.get("role"):
        trimmed["role"] = event.get("role")
    # Заголовок разговора — только если он что-то добавляет к самому тексту.
    if conversation and conversation[:60] != text[:60]:
        trimmed["conversation"] = conversation[:80]
    if event.get("title"):
        trimmed["title"] = str(event.get("title"))[:120]
    return trimmed


def _inbox_row_for_llm(row: dict[str, Any]) -> dict[str, Any]:
    """Одна строка входящих в том виде, в каком она полезна модели.

    Замерено на живом архиве 2026-08-02: `inbox_list` отдавал модели 11 936
    знаков — почти весь бюджет инструмента — и бо́льшую часть занимала внутренняя
    кухня, сериализованная в строку: `enrichment_version`, `policy_version`,
    `promotion_assessment` со штрафами и сигналами, `suggestions_json` целиком.
    Ответить на вопрос «что у меня во входящих» это не помогает, зато вытесняет
    из контекста сами материалы и оплачивается временем ответа.

    Хуже: на 11 900-м знаке результат обрезается посреди структуры, и последние
    строки списка не доходят вовсе — молча.
    """
    suggestions = row.get("suggestions_json")
    if isinstance(suggestions, str):
        try:
            suggestions = json.loads(suggestions)
        except (TypeError, ValueError):
            suggestions = {}
    suggestions = suggestions if isinstance(suggestions, dict) else {}
    tags = row.get("suggested_tags_json")
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (TypeError, ValueError):
            tags = []
    title = str(row.get("title") or suggestions.get("title") or "").strip()
    preview = str(row.get("preview") or row.get("content") or suggestions.get("summary") or "").strip()
    return {
        "id": row.get("id"),
        "status": row.get("status"),
        "title": title[:120],
        # Достаточно, чтобы человек узнал материал, и мало, чтобы весь список
        # уместился целиком: с превью в 300 знаков двадцать строк снова упирались
        # в предел инструмента и обрезались посреди структуры.
        "preview": preview[:180],
        "tags": [str(tag) for tag in (tags if isinstance(tags, list) else [])][:8],
        "importance": suggestions.get("importance"),
        "kind": suggestions.get("knowledge_kind"),
        "created_at": row.get("created_at"),
    }


_WEB_SOURCE_STRING_LIMITS = {
    "id": 120,
    "url": 800,
    "title": 240,
    "search_title": 240,
    "snippet": 320,
    "source": 80,
    "error": 200,
}


def _web_research_for_llm(data: dict[str, Any]) -> tuple[str, bool]:
    """Render every research source inside one bounded, valid JSON envelope.

    Cutting the joined JSON at its head made the first long page consume the
    entire tool budget: measured on three equal 20k fixtures, only 1/3 source URLs
    reached the model and the JSON itself was invalid. Metadata is bounded first,
    then the remaining text budget is shared between sources so a later source can
    never disappear merely because an earlier one was long.
    """

    raw_sources = data.get("sources")
    if not isinstance(raw_sources, list):
        return json.dumps(data, ensure_ascii=False, indent=2), False

    root: dict[str, Any] = {}
    for key in (
        "query",
        "summary",
        "requested_sources",
        "completed_sources",
        "timed_out_sources",
        "failed_sources",
        "search_timed_out",
    ):
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, str):
            value = value[: (1_000 if key == "query" else 600)]
        root[key] = value

    sources: list[dict[str, Any]] = []
    source_texts: list[str] = []
    for raw_source in raw_sources:
        source = raw_source if isinstance(raw_source, dict) else {"text": str(raw_source)}
        compact: dict[str, Any] = {}
        for key in (
            "id",
            "url",
            "title",
            "text_length",
            "status_code",
            "error",
            "truncated",
            "search_title",
            "snippet",
            "source",
        ):
            if key not in source:
                continue
            value = source[key]
            if isinstance(value, str):
                value = value[: _WEB_SOURCE_STRING_LIMITS.get(key, 200)]
            compact[key] = value
        compact["text"] = ""
        sources.append(compact)
        source_texts.append(str(source.get("text") or ""))

    root["sources"] = sources
    encoded_empty = json.dumps(root, ensure_ascii=False, indent=2)
    if len(encoded_empty) > _LLM_TOOL_PAYLOAD_MAX_CHARS:
        # Pathological metadata must not make later sources disappear either.
        # Keep identity/status for every source and spend the rest on page text.
        sources = [
            {
                **({"id": str(source.get("id") or "")[:80]} if source.get("id") else {}),
                "url": str(source.get("url") or "")[:500],
                "title": str(source.get("title") or source.get("search_title") or "")[:100],
                "status_code": source.get("status_code"),
                "error": str(source.get("error") or "")[:100],
                "truncated": bool(source.get("truncated")),
                "text": "",
            }
            for source in (item if isinstance(item, dict) else {} for item in raw_sources)
        ]
        root["sources"] = sources
        encoded_empty = json.dumps(root, ensure_ascii=False, indent=2)

    remaining = max(0, _LLM_TOOL_PAYLOAD_MAX_CHARS - len(encoded_empty) - 64)
    per_source = remaining // max(1, len(sources))
    # Резать надо ВОКРУГ СОВПАДЕНИЯ, а не с головы, и здесь — тоже. `to_dict`
    # уже отдаёт выдержку по запросу, но её потолок (12 000) больше того, что
    # реально влезает в бюджет слота (на трёх источниках это ~3600 знаков), и
    # прежний `text[:per_source]` отрезал начало ЭТОЙ выдержки — то есть
    # починка, сделанная на первом шаге, гасилась на втором. Замерено:
    # искомое место на позиции 9500 доходило до модели через `to_dict` и
    # терялось здесь.
    query_for_snippet = str(root.get("query") or "").strip()
    compacted = False
    for source, text in zip(sources, source_texts, strict=False):
        if query_for_snippet and len(text) > per_source:
            source["text"] = best_snippet(query_for_snippet, text, max_chars=per_source)
        else:
            source["text"] = text[:per_source]
        if len(text) > len(source["text"]):
            source["truncated"] = True
            compacted = True

    if compacted:
        root["llm_truncated_sources"] = sum(
            1 for source, text in zip(sources, source_texts, strict=False) if len(text) > len(source["text"])
        )

    encoded = json.dumps(root, ensure_ascii=False, indent=2)
    # Quotes, slashes and control characters expand during JSON encoding. Shrink
    # every source proportionally until the serialized envelope itself fits.
    while len(encoded) > _LLM_TOOL_PAYLOAD_MAX_CHARS and any(source["text"] for source in sources):
        ratio = max(0.1, (_LLM_TOOL_PAYLOAD_MAX_CHARS - 64) / len(encoded))
        for source in sources:
            text = str(source["text"])
            source["text"] = text[: max(0, int(len(text) * ratio) - 4)]
            source["truncated"] = True
        compacted = True
        encoded = json.dumps(root, ensure_ascii=False, indent=2)

    return encoded, compacted


_MEMORY_GRAPH_CONTEXT_MAX_CHARS = 3_200
_TEMPORAL_SNAPSHOT_FIELDS = (
    "known_at",
    "known_at_floor",
    "history_complete",
    "identity_basis",
    "temporal_basis",
)
_ENTITY_LOOKUP_RELATION_CAP = 12


def _validated_known_at_preflight(raw: Any, *, known_at: str) -> dict[str, Any]:
    """Validate storage's status and derive the exact five-field public contract."""

    if not isinstance(raw, Mapping):
        raise ValueError("relation-history status is missing")
    boundary = raw.get("known_at")
    floor = raw.get("known_at_floor")
    if not known_at or not isinstance(boundary, str) or boundary != known_at:
        raise ValueError("relation-history status returned a different known_at boundary")
    if normalize_known_at(boundary) != boundary:
        raise ValueError("relation-history status returned a non-normalized known_at boundary")
    if not isinstance(floor, str) or normalize_known_at(floor, reject_future=False) != floor:
        raise ValueError("relation-history status returned an invalid completeness floor")
    if floor > known_at:
        raise ValueError("known_at precedes the relation-history completeness floor")
    if raw.get("history_complete") is not True:
        raise ValueError("relation-history snapshot is incomplete")
    if raw.get("identity_basis") != "current_names":
        raise ValueError("relation-history snapshot has an unsupported identity basis")
    temporal_basis = raw.get("temporal_basis")
    if temporal_basis not in (None, "", "bitemporal"):
        raise ValueError("relation-history snapshot has an unsupported temporal basis")
    return {
        "known_at": known_at,
        "known_at_floor": floor,
        "history_complete": True,
        "identity_basis": "current_names",
        "temporal_basis": "bitemporal",
    }


def _assert_temporal_snapshot_agrees(
    raw: Any,
    expected: Mapping[str, Any],
    *,
    label: str,
) -> Mapping[str, Any]:
    """Require a complete downstream contract, never an overlay on current data."""

    if not isinstance(raw, Mapping):
        raise ValueError(f"{label} is not a mapping")
    for key in _TEMPORAL_SNAPSHOT_FIELDS:
        if key not in raw:
            raise ValueError(f"{label} is missing {key}")
        if type(raw[key]) is not type(expected[key]) or raw[key] != expected[key]:
            raise ValueError(f"{label} disagrees on {key}")
    return raw


def _assert_snapshot_as_of(raw: Any, *, as_of: str, label: str) -> Mapping[str, Any]:
    """Require the normalized valid-time boundary to be echoed exactly."""

    if not isinstance(raw, Mapping):
        raise ValueError(f"{label} is not a mapping")
    if "as_of" not in raw:
        raise ValueError(f"{label} is missing as_of")
    if raw["as_of"] != as_of:
        raise ValueError(f"{label} disagrees on as_of")
    return raw


def _assert_valid_time_basis(raw: Any, *, label: str) -> Mapping[str, Any]:
    """Reject an as-of response that does not explicitly name valid-time semantics."""

    if not isinstance(raw, Mapping):
        raise ValueError(f"{label} is not a mapping")
    if raw.get("temporal_basis") != "valid_time":
        raise ValueError(f"{label} disagrees on temporal_basis")
    return raw


def _entity_lookup_relation_projection(
    edge: Mapping[str, Any],
    nodes: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    """One narrow human-readable historical edge, without raw provenance."""

    relation_type = edge.get("relation_type")
    source_name = edge.get("source_name")
    target_name = edge.get("target_name")
    valid_from = edge.get("valid_from")
    valid_to = edge.get("valid_to")
    if not isinstance(source_name, str):
        source_name = (nodes.get(str(edge.get("source_entity_id"))) or {}).get("name")
    if not isinstance(target_name, str):
        target_name = (nodes.get(str(edge.get("target_entity_id"))) or {}).get("name")
    return {
        "type": relation_type[:80] if isinstance(relation_type, str) else "",
        "source": source_name[:200] if isinstance(source_name, str) else "",
        "target": target_name[:200] if isinstance(target_name, str) else "",
        "valid_from": valid_from[:32] if isinstance(valid_from, str) else "",
        "valid_to": valid_to[:32] if isinstance(valid_to, str) else "",
    }


def _memory_graph_context_for_llm(
    raw: Any,
    *,
    query: str,
    as_of: str,
    known_at: str = "",
) -> dict[str, Any]:
    """Bound a memory-search graph snapshot before it enters a tool result.

    Retrieval already publishes an allowlisted graph projection, but the kernel
    also runs with test/legacy searchers.  Treat their return as untrusted: applying
    the canonical projection here keeps raw relation metadata and long evidence text
    out of the model's tool envelope.  A structural six-path/four-edge cap is
    followed by a serialized-size cap so document excerpts still fit the fixed
    12k tool budget.
    """

    source = raw if isinstance(raw, Mapping) else {}
    source_query = source.get("query")
    effective_query = (
        source_query[:700] if isinstance(source_query, str) else query[:700] if isinstance(query, str) else ""
    )
    bounded = _public_graph_context(
        source,
        query=effective_query,
        as_of=as_of,
        known_at=known_at,
        expanded=bool(source.get("expanded")),
    )
    paths = list(bounded.get("paths") or [])
    shown_paths = paths[:6]
    bounded["paths"] = shown_paths
    try:
        matched = max(len(paths), int(bounded.get("paths_matched_at_least") or len(paths)))
    except (TypeError, ValueError):
        matched = len(paths)
    bounded["paths_matched_at_least"] = matched
    bounded["paths_truncated"] = bool(bounded.get("paths_truncated")) or matched > len(shown_paths)
    # Row caps alone are not a byte budget: six four-hop paths with maximal IDs
    # can still exceed the whole tool envelope.  Remove redundant flat projections
    # first, then tail paths, always as complete JSON objects.  ToolResult's final
    # character guard must never be the first thing that makes this structure fit,
    # because slicing serialized JSON would make it unparsable.
    while len(json.dumps(bounded, ensure_ascii=False)) > _MEMORY_GRAPH_CONTEXT_MAX_CHARS:
        relations = bounded.get("relations")
        entities = bounded.get("entities")
        nodes = bounded.get("nodes")
        roots = bounded.get("roots")
        current_paths = bounded.get("paths")
        if isinstance(relations, list) and relations:
            relations.pop()
        elif isinstance(entities, list) and entities:
            entities.pop()
        elif isinstance(nodes, list) and nodes:
            nodes.pop()
        elif isinstance(roots, list) and roots:
            roots.pop()
        elif isinstance(current_paths, list) and current_paths:
            current_paths.pop()
            bounded["paths_truncated"] = True
        else:
            break
    # Search may have repaired the query.  This is the effective string that built
    # the snapshot, not a reconstruction from the original tool arguments.
    return bounded


# Длина выдержки в ответе инструмента. Десять результатов по 600 знаков — это 6 000
# плюс обвязка, то есть половина бюджета в 12 000: остаётся место и на ответ модели, и
# на второй вызов. Прежде сюда уходили тела документов, и одного среднего (16 565
# знаков на этом архиве) хватало, чтобы переполнить бюджет целиком.
_TOOL_EXCERPT_CHARS = 600

#: Сколько знаков запроса уходит НАРУЖУ, в чужой поисковик.
#:
#: Замерено на стенде: прямой вызов инструмента отправлял реплику целиком — 371
#: знак разговорного текста про закупку оборудования, вместе с обстоятельствами,
#: которые к поиску отношения не имеют. Поисковику больше двух сотен знаков и не
#: нужно, а цена утечки растёт с каждым: в журнале остаётся только хеш, и что
#: именно ушло, владелец потом не узнает.
_MAX_OUTBOUND_QUERY_CHARS = 200

#: Слова, которые именем человека не бывают. Без них поиск по графу срабатывал бы
#: на «что» и «известно» в каждом втором вопросе. Список тот же по смыслу, что у
#: одноимённой проверки в `agent_runtime`, и живёт здесь отдельно намеренно: ядро
#: не должно зависеть от слоя, который его вызывает.
_NOT_A_NAME_OUTBOUND = frozenset(
    {
        "что",
        "кто",
        "где",
        "когда",
        "сколько",
        "какой",
        "какая",
        "какие",
        "какое",
        "почему",
        "зачем",
        "чем",
        "куда",
        "откуда",
        "известно",
        "расскажи",
        "покажи",
        "напомни",
        "найди",
        "поищи",
        "посмотри",
        "скажи",
        "можешь",
        "нужно",
        "хочу",
        "пожалуйста",
        "сегодня",
        "вчера",
        "завтра",
        "сейчас",
        "потом",
        "тогда",
        "документ",
        "документы",
        "документов",
        "файл",
        "файлы",
        "база",
        "базе",
        "архив",
        "архиве",
        "интернет",
        "интернете",
        "поиск",
        "погода",
        "курс",
        "новости",
        "цена",
        "стоит",
        "такое",
        "такой",
        "этот",
        "эта",
        "тебе",
        "меня",
        "него",
        "неё",
        "нас",
        "вас",
        "them",
        "what",
        "who",
        "when",
        "where",
    }
)


class ExecutionKernel:
    """One immutable registry; user identity is supplied per invocation."""

    def __init__(
        self,
        authorization: AuthorizationService | None = None,
        settings: FridaySettings | None = None,
    ) -> None:
        self.authorization = authorization
        self.settings = settings
        self.storage: FridayStorage | None = None
        self.kg: KnowledgeGraph | None = None
        self.web_surfer: WebSurfer | None = None
        self.ingestion: IngestionPipeline | None = None
        self.executive: ExecutiveService | None = None
        self.searcher: Any = None
        self._tools: dict[str, ToolSpec] = {}
        self._register_specs()

    def bind_services(
        self,
        storage: FridayStorage,
        kg: KnowledgeGraph,
        web_surfer: WebSurfer,
        ingestion: IngestionPipeline,
        searcher: Any = None,
    ) -> None:
        self.storage = storage
        self.kg = kg
        self.web_surfer = web_surfer
        self.ingestion = ingestion
        # Тот же гибридный поиск, что у контекстного пути. Ядро его не получало, и
        # инструмент памяти работал на префиксном FTS: без эмбеддингов, без морфологии.
        self.searcher = searcher
        handlers: dict[str, Handler] = {
            # Компенсация зависит только от хранилища, поэтому привязывается здесь,
            # а не в `bind_executive`: заявка на откат приходит человеку и без
            # исполнительной службы (её создаёт разбор оборвавшихся шагов), и
            # инструмент, привязанный к службе, отвечал бы «Инструмент недоступен»
            # ровно тем сборкам, где служба не поднята.
            "mission_compensation": self._mission_compensation,
            "memory_search": self._memory_search,
            "message_search": self._message_search,
            "memory_save": self._memory_save,
            "web_search": self._web_search,
            "web_fetch": self._web_fetch,
            "web_research": self._web_research,
            "entity_lookup": self._entity_lookup,
            "relation_end": self._relation_end,
            "data_sources": self._data_sources,
            "data_schema": self._data_schema,
            "data_query": self._data_query,
            "entity_create": self._entity_create,
            "entity_link": self._entity_link,
            "kg_stats": self._kg_stats,
            "make_file": self._make_file,
            "collect_files": self._collect_files,
            "what_happened": self._what_happened,
            "upcoming": self._upcoming,
            "remind": self._remind,
            "list_tags": self._list_tags,
            "speak": self._speak,
            "resolve_duplicates": self._resolve_duplicates,
            "conflict_list": self._conflict_list,
            "conflict_decide": self._conflict_decide,
            "entity_merge_decide": self._entity_merge_decide,
            "entity_merge_undo": self._entity_merge_undo,
            "inbox_list": self._inbox_list,
            "user_activity": self._user_activity,
            "user_knowledge_search": self._user_knowledge_search,
            "code_run": self._code_run,
        }
        for name, handler in handlers.items():
            self._tools[name].handler = handler

    # Compatibility alias: unlike the old implementation this never captures
    # a user and is therefore safe in a multi-user process.
    def bind_session(self, user_id: str, storage, kg, web_surfer, ingestion) -> None:
        del user_id
        self.bind_services(storage, kg, web_surfer, ingestion)

    def bind_executive(self, executive: ExecutiveService) -> None:
        """Attach the executive so the agent can propose missions for review.

        Bound after construction to avoid a service/kernel import cycle; until
        this runs the ``mission_propose`` tool stays invisible (no handler).
        """
        self.executive = executive
        self._tools["mission_propose"].handler = self._mission_propose
        # Расхождение деклараций риска с гейтом — ошибка СБОРКИ, а не поведения.
        #
        # Проверять её здесь, а не при первом опасном вызове: инструмент,
        # объявивший себя опасным и не попавший под гейт, обходит человека молча,
        # и узнать об этом можно только постфактум — по сделанному.
        self.assert_risk_declarations_agree()

    def register(self, tool: ToolSpec) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def assert_risk_declarations_agree(self) -> None:
        """Словарь предикатов обязан ТОЧНО совпадать с объявленными классами риска.

        Комментарий у `ToolSpec.risk` обещает, что обязательное поле не даёт
        новому опасному инструменту пройти fail-open. Обещание было пустым:
        решение читало только `HIGH_RISK_TOOLS` по имени, а поле в нём не
        участвовало. Множества совпадали случайно, и это совпадение и было всей
        защитой.

        Здесь оно перестаёт быть случайным. Расхождение видно НА СТАРТЕ, а не при
        первом опасном вызове, и обе стороны названы отдельно:

            объявлен `high`, предиката нет — забыт предикат;
            предикат есть, а инструмент опасным себя не считает — забыта
            декларация, и гейт сработает там, где его не ждали.

        Словарь при этом остаётся и остаётся нужным: риск живёт в АРГУМЕНТАХ, а
        не в инструменте — `entity_merge_decide` с `decision=reject` безопасен, с
        `accept` нет. Он перестаёт быть источником политики и становится её
        реализацией.
        """
        declared = {name for name, tool in self._tools.items() if tool.risk == "high"}
        with_predicate = set(HIGH_RISK_TOOLS)
        missing_predicate = sorted(declared - with_predicate)
        missing_declaration = sorted(name for name in with_predicate - declared if name in self._tools)
        problems = []
        if missing_predicate:
            problems.append("объявлены high, но предиката нет: " + ", ".join(missing_predicate))
        if missing_declaration:
            problems.append("предикат есть, а класс риска не high: " + ", ".join(missing_declaration))
        if problems:
            raise ValueError("Декларации риска разошлись с гейтом — " + "; ".join(problems))

    def get_tool(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def get_tool_names(
        self,
        actor: ActorContext | None = None,
        *,
        execution_scope: str = "dialogue",
    ) -> list[str]:
        return [tool.name for tool in self._visible_tools(actor, execution_scope=execution_scope)]

    #: Какие инструменты уместны при каком виде вопроса. Остальные остаются
    #: доступны, но описаны одной строкой — см. `ToolSpec.to_openai(brief=True)`.
    #: Списки намеренно щедрые: цена лишнего подробного описания — сотня токенов,
    #: цена недостающего — несделанное дело.
    _RELEVANT_TOOLS = {
        "интернет": {"web_search", "web_fetch", "web_research", "speak", "make_file", "remind"},
        "знание": {"speak", "make_file", "remind", "memory_search"},
        "архив": {
            "memory_search",
            "message_search",
            "what_happened",
            "upcoming",
            "list_tags",
            "kg_stats",
            "entity_lookup",
            "user_activity",
            "user_knowledge_search",
            "inbox_list",
            "make_file",
            "collect_files",
            "speak",
            "remind",
        },
        "материал": {"memory_save", "entity_create", "entity_link", "inbox_list", "make_file"},
        # Поручение: человек просит СДЕЛАТЬ. Подробные описания получают те
        # инструменты, что меняют мир, — замерено, что именно они простаивают:
        # шесть из десяти мутирующих не срабатывали ни разу за всё время.
        "действие": {
            "remind",
            "memory_save",
            "entity_create",
            "entity_link",
            "relation_end",
            "speak",
            "make_file",
            "collect_files",
            "mission_propose",
        },
        # Просьба о файле — это и «сочини документ» (make_file), и «собери
        # присланное» (collect_files). Какой из двух, решает модель по формулировке.
        "файл": {"make_file", "collect_files", "memory_search", "what_happened", "speak"},
        # Быт: человек говорит о себе, а не спрашивает систему.
        #
        # Вида здесь не было вовсе, а «неизвестный вид» означает полные описания
        # ВСЕХ инструментов — то есть на «устал сегодня» модель видела ленту
        # событий во всей красе и брала её. Замерено 2026-08-03: три раза из трёх,
        # и ответ получался пересказом рабочего дня человека, включая то, что
        # писал названный по имени коллега. Это ровно та жалоба, с которой всё
        # начиналось: «хочет человек поболтать, а она в архив лезет».
        #
        # Оставлены те, что в разговоре законны: «напомни завтра» посреди
        # болтовни — настоящий случай, ради которого набор вообще не урезается, а
        # только сокращаются описания.
        "быт": {"remind", "speak"},
        # Про человека отвечает надзор, а не архив. Соседнее правило уже обнуляет
        # найденные документы на таком вопросе — описания приводятся в согласие.
        "человек": {"user_activity", "user_knowledge_search", "message_search", "speak"},
        # Указание о поведении записано ДО хода модели; звать ей тут нечего.
        "правило": {"speak"},
    }

    #: Инструменты, которые на этом виде не просто описываются коротко, а НЕ
    #: ПРЕДЛАГАЮТСЯ вовсе.
    #:
    #: Исключение из общего правила «набор не урезаем, только сокращаем
    #: описания», и оно сделано по замеру, а не по догадке. На «устал сегодня»
    #: модель звала ленту событий три раза из трёх; после сокращения описаний —
    #: два из трёх. Ответ при этом получался пересказом рабочего дня человека,
    #: включая то, что писал названный по имени коллега, — на бытовую реплику в
    #: два слова.
    #:
    #: Отнимается ровно то, что читает архив, и ровно на бытовом вердикте.
    #: Противоречия с прежним решением здесь нет: на этом же ходу найденные
    #: документы УЖЕ выброшены заслоном по тому же вердикту, и оставлять модели
    #: возможность добрать их инструментом — значит спорить с собой внутри
    #: одного хода. Довод, ради которого набор не урезают («напомни завтра»
    #: посреди болтовни), сохранён дословно: `remind`, `speak`, `make_file`,
    #: `memory_save` остаются на месте.
    _WITHHELD_TOOLS = {
        "быт": {
            "what_happened",
            "upcoming",
            "memory_search",
            "message_search",
            "list_tags",
            "kg_stats",
            "entity_lookup",
            "user_activity",
            "user_knowledge_search",
            "inbox_list",
            "collect_files",
        },
    }

    def get_tool_definitions(
        self,
        actor: ActorContext | None = None,
        *,
        topic: str = "",
        execution_scope: str = "dialogue",
    ) -> list[dict[str, Any]]:
        """Описания инструментов; `topic` — вид вопроса от арбитра намерения.

        Без `topic` (или при неизвестном виде) все описания идут полностью — так
        было всегда, и это безопасное умолчание.
        """
        kind = str(topic or "").strip().casefold()
        relevant = self._RELEVANT_TOOLS.get(kind)
        withheld: set[str] | frozenset[str] = self._WITHHELD_TOOLS.get(kind, frozenset())
        return [
            tool.to_openai(brief=relevant is not None and tool.name not in relevant)
            for tool in self._visible_tools(actor, execution_scope=execution_scope)
            if tool.name not in withheld
        ]

    def _visible_tools(
        self,
        actor: ActorContext | None,
        *,
        execution_scope: str = "dialogue",
    ) -> list[ToolSpec]:
        if execution_scope not in EXECUTION_SCOPES:
            return []
        if actor is None:
            try:
                actor = current_actor()
            except Exception:
                return []
        if self.authorization is None:
            # Deny-by-default: a kernel wired without an authorization service
            # exposes nothing instead of everything.
            return []
        visible: list[ToolSpec] = []
        for tool in self._tools.values():
            if execution_scope not in tool.allowed_execution_scopes:
                continue
            if tool.name == "code_run" and not (self.settings and self.settings.code_execution_enabled):
                continue
            if not self.authorization.authorize(actor, tool.security_id).allowed:
                continue
            if tool.handler:
                visible.append(tool)
        return visible

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: ActorContext | None = None,
        execution_scope: str = "dialogue",
    ) -> ToolResult:
        actor = actor or current_actor()
        details = self._audit_details(name, arguments)
        if self.authorization is None:
            # Fail closed: without an authorization service no capability can
            # be verified, so no tool may run — never the other way around.
            await self._audit(actor, name, False, "no_authorization_service", details=details)
            return ToolResult(name, False, error="Execution kernel has no authorization service")
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(name, False, error="Unknown tool")
        if not tool.handler:
            return ToolResult(name, False, error="Tool is not initialized")
        if execution_scope not in EXECUTION_SCOPES or execution_scope not in tool.allowed_execution_scopes:
            await self._audit(
                actor,
                name,
                False,
                "execution_scope_denied",
                details={
                    **details,
                    "execution_scope": (
                        execution_scope if execution_scope in EXECUTION_SCOPES else "invalid"
                    ),
                },
            )
            return ToolResult(name, False, error="Tool is unavailable in this execution scope")
        try:
            self.authorization.require(actor, tool.security_id)
        except AuthorizationError:
            await self._audit(actor, name, False, "authorization_denied", details=details)
            return ToolResult(name, False, error="Authorization denied")
        if name == "code_run" and not (self.settings and self.settings.code_execution_enabled):
            await self._audit(actor, name, False, "disabled", details=details)
            return ToolResult(name, False, error="Code execution is disabled by configuration")

        # Решение читает ДЕКЛАРАЦИЮ инструмента, а не только словарь имён.
        #
        # Объявлен `high`, а предиката нет — закрываемся: спрашиваем человека.
        # Обратный порядок («нет в словаре — значит можно») и есть тот самый
        # fail-open, от которого обязательное поле якобы защищает. Инвариант
        # `assert_risk_declarations_agree` не даёт такому инструменту дожить до
        # запуска, но исполнение всё равно обязано быть безопасным само по себе:
        # инвариант проверяется на старте, а регистрация бывает и динамической.
        # Способность, помеченная «требует человека», задерживает ЛЮБОЙ свой
        # инструмент — независимо от класса риска самого инструмента. Это и есть
        # смысл пометки: право говорит «этому актору можно», а пометка — «но не
        # молча». Раньше поле не читал никто, и объявление его роняло старт.
        if self._capability_requires_person(tool.security_id):
            return await self._request_approval(actor, name, arguments or {}, details)
        needs_person = HIGH_RISK_TOOLS.get(name)
        if tool.risk == "high" and needs_person is None:
            LOGGER.warning("tool %s объявлен high, но предиката риска нет — требуем человека", name)
            return await self._request_approval(actor, name, arguments or {}, details)
        if needs_person and needs_person(arguments or {}):
            return await self._request_approval(actor, name, arguments or {}, details)

        timeout = 30
        if self.settings:
            timeout = max(1, self.settings.code_execution_timeout_sec if name == "code_run" else 30)
        # Инструмент, МЕНЯЮЩИЙ данные, получает запись о начале ДО вызова.
        #
        # Разбор Codex §14, воспроизведено: аудит писался только после возврата
        # обработчика, поэтому обрыв через `BaseException` — отмена задачи,
        # остановка процесса — оставлял эффект в базе и НОЛЬ записей о вызове. Со
        # стороны это выглядит как «инструмент не звали».
        #
        # Оборванная пара «начал / нет конца» сама является доказательством
        # незавершённости, и в этом весь смысл: восстановить её постфактум нельзя,
        # а увидеть — можно.
        #
        # Наблюдающие инструменты не трогаются: у чтения нет эффекта, о котором
        # можно не знать, а лишняя пара записей на каждый поиск засоряет журнал,
        # в котором ищут настоящие действия.
        changes_data = tool.risk in {"mutate", "high"}
        # Аргументы сверяются с сигнатурой ДО записи «начал».
        #
        # Иначе «инструмент вызван с чужим именем поля» неотличимо от «инструмент
        # упал на середине»: и то и другое приходит сюда как `TypeError`, только
        # первое случается ДО первой строки обработчика, а второе — после того,
        # как он мог что-то записать. Разница видна не по типу исключения, а по
        # тому, дошло ли дело до вызова, и здесь она устанавливается точно.
        #
        # Модель ошибается именем поля регулярно, и говорить ей на это «работа
        # НАЧАЛАСЬ, проверьте, не выполнено ли действие» — значит приучить её (и
        # человека) не верить предупреждению, которое в остальных случаях верно.
        try:
            inspect.signature(tool.handler).bind(actor=actor, **(arguments or {}))
        except TypeError:
            await self._audit(actor, name, False, "invalid_arguments", details=details)
            return ToolResult(name, False, error="Invalid tool arguments: TypeError")
        if changes_data:
            await self._audit(actor, name, True, "started", details=details)
        try:
            async with asyncio.timeout(timeout):
                data = await tool.handler(actor=actor, **(arguments or {}))
            await self._audit(actor, name, True, "ok", details=details)
            attachment = None
            # A handler that produces a binary side artifact (currently only
            # `speak`) marks it with this key instead of returning it as part of
            # `data`, so it never reaches the model via `to_llm_message()`.
            if isinstance(data, dict) and "_attachment" in data:
                data = dict(data)
                attachment = data.pop("_attachment")
            return ToolResult(name, True, data=data, attachment=attachment)
        except TimeoutError:
            # Таймаут наступает ПОСЛЕ начала работы, а значит эффект мог случиться.
            #
            # Прежний текст «Tool execution timed out» читается человеком и моделью
            # как «ничего не вышло», и следующий шаг — повторить. Эффект при этом
            # уже есть, и повтор делает его вторым. Особенно у отменённого
            # `asyncio.to_thread`: ожидание прервано, а поток может продолжать
            # писать в базу.
            #
            # Наблюдающему инструменту таймаут ничем не грозит: читать нечего
            # дважды, и там остаётся прежний честный отказ.
            if changes_data:
                await self._audit(actor, name, False, "uncertain", details=details)
                return ToolResult(
                    name,
                    False,
                    error=(
                        "Инструмент не ответил вовремя, и НЕИЗВЕСТНО, успел ли он "
                        "выполнить действие. Проверьте результат, прежде чем повторять."
                    ),
                )
            await self._audit(actor, name, False, "timeout", details=details)
            return ToolResult(name, False, error="Tool execution timed out")
        except Exception as exc:
            # Тип исключения НЕ доказывает, что эффекта не было.
            #
            # Здесь стояло разделение по типу: `TypeError`/`ValueError` считались
            # разбором аргументов, то есть отказом до эффекта, а прочие — обычным
            # сбоем. Оба утверждения о причине ложны, когда исключение прилетело
            # из середины обработчика: тот мог записать данные и упасть следующей
            # строкой. «Invalid tool arguments» в этом случае ложно вдвойне — оно
            # называет причиной аргументы. Указано внешним разбором (Сол,
            # 2026-08-04).
            #
            # Различает не тип исключения, а РИСК инструмента. У наблюдающего
            # эффекта нет вовсе, и там прежний честный отказ остаётся: если
            # неизвестно всё, слово «неизвестно» теряет смысл и его перестают
            # читать. У меняющего данные — сказано, что работа НАЧАЛАСЬ, и не
            # сказано, чем кончилась.
            #
            # Слово «НЕИЗВЕСТНО» намеренно оставлено таймауту: там неизвестен сам
            # исход, здесь известен сбой и неизвестны его последствия.
            LOGGER.warning("Tool %s failed (%s)", name, type(exc).__name__)
            if changes_data:
                # The durable reason is a closed structural code.  Exception
                # class names are useful in the bounded runtime log above, but
                # they are not part of the content-free audit schema (and a
                # custom exception may have a caller-controlled class name).
                await self._audit(actor, name, False, "failed_after_start", details=details)
                return ToolResult(
                    name,
                    False,
                    error=(
                        f"Инструмент {name} прервался ошибкой ({type(exc).__name__}) уже НАЧАВ "
                        "работу. Проверьте, не выполнено ли действие, прежде чем повторять."
                    ),
                )
            if isinstance(exc, TypeError | ValueError):
                await self._audit(actor, name, False, "invalid_arguments", details=details)
                return ToolResult(
                    name,
                    False,
                    error=f"Invalid tool arguments: {type(exc).__name__}",
                )
            await self._audit(actor, name, False, type(exc).__name__, details=details)
            return ToolResult(name, False, error=f"Tool failed: {type(exc).__name__}")

    async def _request_approval(
        self,
        actor: ActorContext,
        name: str,
        arguments: dict[str, Any],
        details: dict[str, Any],
    ) -> ToolResult:
        """Опасное действие не выполняется, а становится заявкой на подтверждение.

        Возвращается ОТКАЗ, а не успех: действие не произошло, и `success=True`
        здесь был бы ровно тем ложным завершением, которое спека запрещает. Текст
        отказа прямо говорит, что повтор ничего не изменит, — иначе модель, увидев
        неудачу, попробует ещё раз, и человек получит очередь одинаковых заявок.
        """
        storage, _, _, _ = self._require_services()
        try:
            approval = await run_blocking(
                storage.create_action_approval,
                actor.user_id,
                tool=name,
                payload=arguments,
                summary=self._approval_summary(storage, actor.user_id, name, arguments),
                risk="high",
                # Кто ПРОСИЛ — человек, а не способ входа. В `identity_id` лежит
                # идентификатор токена или связанной телеграм-личности, и заявка
                # разъезжалась бы по токенам, которыми человек входил. По этому же
                # полю теперь держится личная граница списка и решения.
                requested_by=actor.own_id,
            )
        except Exception as exc:  # noqa: BLE001 - отказ в заявке не должен выполнять действие
            LOGGER.warning(
                "Could not create an approval request for %s (%s)",
                name,
                type(exc).__name__,
            )
            await self._audit(actor, name, False, "approval_request_failed", details=details)
            return ToolResult(
                name,
                False,
                error=f"Не удалось запросить подтверждение ({type(exc).__name__}); действие не выполнено",
            )
        await self._audit(
            actor, name, False, "approval_required", details={**details, "approval_id": approval["id"]}
        )
        await run_blocking(self._notify_pending_approval, storage, actor, approval)
        return ToolResult(
            name,
            False,
            data={
                "status": "approval_required",
                "approval_id": approval["id"],
                "summary": approval["summary"],
            },
            error=(
                "Действие НЕ выполнено: оно требует подтверждения человеком. "
                f"Создана заявка: {approval['summary']} "
                "Передай это пользователю своими словами — что именно предлагается и что "
                "подтвердить можно командой /approvals. Не повторяй вызов: повтор ничего "
                "не изменит."
            ),
        )

    def _notify_pending_approval(self, storage, actor: ActorContext, approval: dict[str, Any]) -> None:
        """Заявка сама идёт к человеку, а не ждёт, пока он о ней спросит.

        Без этого механизм был бы наполовину мёртв: действие блокируется, а узнать
        об этом можно только зайдя в `/approvals`. Доставка идёт тем же путём, что
        у проактивных органов, и через тот же предохранитель — в личный чат, не в
        группу: заявка называет, что именно предлагается сделать с личными данными.
        """
        from friday.organs import may_push_to, resolve_chat_id

        try:
            # Чат — у ЧЕЛОВЕКА, а не у арендатора. В общем архиве
            # (`FRIDAY_SHARED_ARCHIVE`) `actor.user_id` у всех один, и заявка
            # уходила бы в чат владельца архива: тот, кто попросил, о своей же
            # заявке не узнавал, а посторонний получал описание действия с
            # чужими данными.
            chat_id = resolve_chat_id(storage, actor.own_id)
            if not chat_id or not self.settings:
                return
            if not may_push_to(self.settings, storage, actor.own_id, chat_id):
                return
            storage.enqueue_notification(
                actor.own_id,
                chat_id,
                f"Нужно ваше решение: {approval['summary']}\n\nОткрыть: /approvals",
                kind="approval",
                dedup_key=f"approval:{approval['id']}",
            )
        except Exception as exc:  # noqa: BLE001 - недоставленное уведомление не отменяет заявку
            LOGGER.warning(
                "Could not queue an approval notification (%s)",
                type(exc).__name__,
            )

    @staticmethod
    def _approval_summary(storage, user_id: str, name: str, arguments: dict[str, Any]) -> str:
        """Одна строка, по которой человек может ПРИНЯТЬ РЕШЕНИЕ, а не опознать запрос.

        Идентификаторы («объединить по кандидату res_7f3a…») не говорят человеку
        ничего: подтверждать по ним — значит подтверждать вслепую, а слепое
        подтверждение хуже отсутствия подтверждения, потому что выглядит как
        контроль. Поэтому здесь читаются имена — те самые, между которыми человек
        и выбирает; при недоступности данных остаётся идентификатор, но тогда это
        видно.
        """
        if name == "entity_merge_decide":
            from friday.storage._graph import (
                _bounded_entity_by_id,
                _bounded_resolution_candidate_by_id,
            )

            candidate = None
            with suppress(Exception):
                candidate = _bounded_resolution_candidate_by_id(
                    storage,
                    str(arguments.get("candidate_id") or ""),
                    user_id,
                )
            if candidate:
                left = right = None
                with suppress(Exception):
                    left = _bounded_entity_by_id(
                        storage,
                        str(candidate.get("entity_a_id") or ""),
                        user_id,
                    )
                    right = _bounded_entity_by_id(
                        storage,
                        str(candidate.get("entity_b_id") or ""),
                        user_id,
                    )
                left_name = str((left or {}).get("name") or candidate.get("entity_a_id") or "?")
                right_name = str((right or {}).get("name") or candidate.get("entity_b_id") or "?")
                confidence = float(candidate.get("confidence") or 0.0)
                return (
                    f"Объединить «{left_name}» и «{right_name}» в один объект "
                    f"(уверенность {confidence:.2f}, {candidate.get('resolution_method') or 'без метода'}). "
                    "Отменить можно в /merges."
                )
            return f"Объединить сущности по кандидату {arguments.get('candidate_id')}"
        if name == "conflict_decide":
            from friday.storage._knowledge import _bounded_knowledge_conflict_by_id

            decision = str(arguments.get("decision") or "")
            conflict = None
            with suppress(Exception):
                conflict = _bounded_knowledge_conflict_by_id(
                    storage,
                    user_id,
                    str(arguments.get("conflict_id") or ""),
                )
            if conflict:
                keep_a = decision == "keep_a"
                winner = str(conflict.get("knowledge_a_title" if keep_a else "knowledge_b_title") or "")
                loser = str(conflict.get("knowledge_b_title" if keep_a else "knowledge_a_title") or "")
                return f"Признать верной запись «{winner[:70]}» и объявить устаревшей «{loser[:70]}»"
            return f"Разрешить противоречие {arguments.get('conflict_id')}: {decision}"
        if name == "code_run":
            code = str(arguments.get("code") or "")
            first_line = next((line.strip() for line in code.splitlines() if line.strip()), "")
            return (
                f"Выполнить код ({len(code)} знаков, sha256 "
                f"{hashlib.sha256(code.encode()).hexdigest()[:12]}): {first_line[:80]}"
            )
        return f"Выполнить {name}"

    async def execute_approved(self, approval_id: str, *, actor: ActorContext | None = None) -> ToolResult:
        """Исполнить действие, которое человек подтвердил. Ровно один раз.

        Заявление (`claim_action_approval`) само по себе является повторной
        авторизацией непосредственно перед побочным эффектом: оно сверяет отпечаток
        аргументов и эпоху политики прав и атомарно переводит заявку в
        «исполняется». Если оно не удалось — действие НЕ выполняется, и это не
        ошибка исполнения, а отказ.

        Права проверяются здесь ЗАНОВО: между решением человека и исполнением
        актор мог лишиться способности, и подтверждение не заменяет права.
        """
        actor = actor or current_actor()
        storage, _, _, _ = self._require_services()
        record = await run_blocking(storage.get_action_approval, approval_id, actor.user_id)
        if not record:
            return ToolResult("approval", False, error="Заявка не найдена")
        name = str(record.get("tool") or "")
        tool = self._tools.get(name)
        if not tool or not tool.handler:
            return ToolResult(name or "approval", False, error="Инструмент недоступен")
        if self.authorization is None:
            return ToolResult(name, False, error="Execution kernel has no authorization service")
        try:
            self.authorization.require(actor, tool.security_id)
        except AuthorizationError:
            await self._audit(actor, name, False, "authorization_denied", details={"approval": approval_id})
            return ToolResult(name, False, error="Authorization denied")

        arguments = dict(record.get("payload") or {})
        claimed = await run_blocking(
            storage.claim_action_approval,
            approval_id,
            actor.user_id,
            payload=arguments,
        )
        if not claimed:
            await self._audit(actor, name, False, "approval_not_claimable", details={"approval": approval_id})
            return ToolResult(
                name,
                False,
                error=(
                    "Подтверждение нельзя использовать: оно не одобрено, уже использовано, "
                    "просрочено или аргументы изменились"
                ),
            )

        details = self._audit_details(name, arguments)
        timeout = 30
        if self.settings:
            timeout = max(1, self.settings.code_execution_timeout_sec if name == "code_run" else 30)
        try:
            async with asyncio.timeout(timeout):
                data = await tool.handler(actor=actor, **arguments)
        except TimeoutError:
            # Отдельно от прочих сбоев: истёкшее время — это НЕИЗВЕСТНЫЙ исход, а
            # не отказ. Обработчик мог довести побочный эффект до конца ровно в тот
            # момент, когда его перестали ждать, поэтому заявка уходит в
            # `uncertain` (сверка человеком), а не в `failed` (можно повторить).
            await run_blocking(
                storage.mark_action_approval_uncertain,
                approval_id,
                actor.user_id,
                error="исполнение не уложилось во время: исход неизвестен",
            )
            await self._audit(actor, name, False, "timeout", details=details)
            return ToolResult(name, False, error="Tool execution timed out")
        except Exception as exc:  # noqa: BLE001 - исход обязан быть записан любым
            LOGGER.warning("Approved tool %s failed (%s)", name, type(exc).__name__)
            await run_blocking(
                storage.finish_action_approval,
                approval_id,
                actor.user_id,
                success=False,
                error=safe_failure_text(exc),
            )
            await self._audit(actor, name, False, type(exc).__name__, details=details)
            return ToolResult(name, False, error=f"Tool failed: {type(exc).__name__}")
        verifier = POSTCONDITIONS.get(name)
        if verifier is not None:
            try:
                verified, reason = await run_blocking(verifier, storage, actor.user_id, arguments)
            except Exception as exc:  # noqa: BLE001 - непроверенное не объявляется сделанным
                verified, reason = False, f"проверку не удалось выполнить: {type(exc).__name__}"
            if not verified:
                # НЕ `failed`: обработчик отработал без ошибки, а факт не
                # подтвердился — значит неизвестно, что именно случилось, и
                # повторять это нельзя. Спека v3 §5: успешный вызов инструмента не
                # доказывает успех задачи.
                await run_blocking(
                    storage.mark_action_approval_uncertain,
                    approval_id,
                    actor.user_id,
                    error=f"постусловие не подтвердилось: {reason}",
                )
                await self._audit(
                    actor, name, False, "postcondition_failed", details={**details, "approval": approval_id}
                )
                return ToolResult(
                    name,
                    False,
                    data=data if isinstance(data, dict) else None,
                    error=(
                        "Инструмент отработал, но результат не подтвердился проверкой: "
                        f"{reason}. Исход неизвестен — проверьте вручную, не повторяйте."
                    ),
                )
        await run_blocking(
            storage.finish_action_approval,
            approval_id,
            actor.user_id,
            success=True,
            result=data if isinstance(data, dict) else {"result": data},
        )
        await self._audit(actor, name, True, "ok_approved", details={**details, "approval": approval_id})
        return ToolResult(name, True, data=data)

    @staticmethod
    def _audit_details(tool_name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        """What a tool invocation should leave behind besides its name.

        `audit_log` is append-only at the database level and not even purge clears it,
        so it holds fingerprints (`sha256` + length), hosts and counts rather than
        content. The same pairing `admin.knowledge.purge` already uses.

        Запрос сюда НЕ кладётся сознательно: в него попадает всё, что человек
        набрал, вплоть до «пароль от роутера …», а журнал не чистится ничем.
        Видимость «что ушло наружу» обеспечивается иначе — строкой в самом
        ответе, которую человек читает сразу и может возразить.

        `code_run` was the first tool fingerprinted this way: without it the audit
        row said only that code ran, and the body was reachable only through the
        truncated tool output. Web tools are the only ones that leave the machine:
        without a fingerprint the owner cannot answer «what did the system fetch
        on my behalf yesterday».
        """
        args = arguments or {}
        if tool_name == "code_run":
            code = args.get("code")
            if not isinstance(code, str):
                return {}
            return {
                "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
                "code_chars": len(code),
            }
        if tool_name in {"web_search", "web_research"}:
            query = args.get("query")
            if not isinstance(query, str):
                return {}
            details: dict[str, Any] = {
                "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
                "query_chars": len(query),
            }
            if tool_name == "web_search":
                max_results = args.get("max_results")
                if isinstance(max_results, int):
                    details["max_results"] = max_results
                site = args.get("site")
                if isinstance(site, str) and site:
                    # A domain can identify a private customer just as easily as
                    # a query can.  The append-only audit gets only a fingerprint
                    # and length; storage projects the digest to a keyed `*_ref`.
                    details["site_sha256"] = hashlib.sha256(site.encode("utf-8")).hexdigest()
                    details["site_chars"] = len(site)
                freshness = args.get("freshness")
                if isinstance(freshness, str) and freshness in SEARCH_FRESHNESS_VALUES and freshness:
                    details["freshness"] = freshness
                for field in ("include_domains", "exclude_domains"):
                    domains = args.get(field)
                    if isinstance(domains, list | tuple):
                        try:
                            canonical_domains = sorted(normalize_search_domains(domains, filter_name=field))
                            fingerprint_body = json.dumps(
                                canonical_domains,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                        except (TypeError, ValueError):
                            # Invalid arguments are audited too.  Keep only a
                            # transient digest and shape, never their values.
                            safe_items = sorted(
                                value if isinstance(value, str) else f"<{type(value).__name__}>"
                                for value in domains
                            )
                            fingerprint_body = json.dumps(
                                safe_items,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                        details[f"{field}_sha256"] = hashlib.sha256(
                            fingerprint_body.encode("utf-8")
                        ).hexdigest()
                        details[f"{field}_count"] = len(domains)
                        details[f"{field}_chars"] = sum(
                            len(value) for value in domains if isinstance(value, str)
                        )
                lang = args.get("lang")
                if isinstance(lang, str):
                    try:
                        canonical_lang = normalize_search_language(lang)
                    except ValueError:
                        canonical_lang = ""
                    if canonical_lang:
                        details["lang"] = canonical_lang
                region = args.get("region")
                if isinstance(region, str):
                    try:
                        canonical_region = normalize_search_region(region)
                    except ValueError:
                        canonical_region = ""
                    if canonical_region:
                        details["region"] = canonical_region
            else:
                max_sources = args.get("max_sources")
                if isinstance(max_sources, int):
                    details["max_sources"] = max_sources
            return details
        if tool_name == "collect_files":
            # Найдено ревью собственных правок 2026-08-03. Инструмент отдаёт
            # ИСХОДНЫЕ файлы, а в общем архиве это файлы всех участников — то
            # есть один запрос уносит чужие личные дела целиком. В журнале при
            # этом оставалось только имя инструмента, и на вопрос «что человек
            # выгрузил вчера» ответить было нечем.
            #
            # Соседний `user_activity` такой след оставляет и прямо обещает это
            # в своём описании; здесь обещания не было, а последствия крупнее.
            #
            # Дни — не содержимое, а рамка запроса: их можно писать целиком, в
            # отличие от текста вопроса (см. оговорку выше про «пароль от
            # роутера»).
            days = args.get("days")
            if not isinstance(days, list):
                return {}
            return {
                "days": [str(day)[:12] for day in days[:12]],
                "day_count": len(days),
            }
        if tool_name == "web_fetch":
            url = args.get("url")
            if not isinstance(url, str):
                return {}
            # Host answers «where did we go»; the path may carry a token in the
            # query string and must never land in an un-purgeable table.
            try:
                host = (urllib.parse.urlsplit(url).hostname or "").casefold()
            except ValueError:
                host = ""
            return {
                "url_host": host,
                "url_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
                "url_chars": len(url),
            }
        return {}

    async def _audit(
        self,
        actor: ActorContext,
        tool_name: str,
        success: bool,
        reason: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        if not self.storage:
            return
        self.storage.log_audit(
            AuditEntry(
                id=new_id("audit"),
                # В общем архиве `user_id` у всех один — это арендатор, а не
                # человек. Кто ДЕЙСТВОВАЛ, отвечает `own_id`, и без него след
                # превратился бы в «кто-то из нас», то есть перестал быть следом.
                # Именно `own_id`, а не `identity_id`: во втором лежит способ
                # входа (идентификатор токена), и один человек с двумя токенами
                # выглядел бы в журнале двумя разными.
                user_id=actor.own_id,
                action="tool.invoke",
                target_type="tool",
                target_id=tool_name,
                after_json={
                    "success": success,
                    "reason": reason,
                    "source": actor.source,
                    **({"tenant": actor.user_id} if actor.shared_tenant else {}),
                    **(details or {}),
                },
            )
        )

    def _capability_requires_person(self, security_id: str) -> bool:  # noqa: D401
        """Спрашивает у авторизации, помечена ли способность как «не молча»."""

        authorization = getattr(self, "authorization", None)
        checker = getattr(authorization, "capability_requires_person", None)
        return bool(checker and checker(security_id))

    def _require_services(self) -> tuple[FridayStorage, KnowledgeGraph, WebSurfer, IngestionPipeline]:
        storage = self.storage
        knowledge_graph = self.kg
        web_surfer = self.web_surfer
        ingestion = self.ingestion
        if storage is None or knowledge_graph is None or web_surfer is None or ingestion is None:
            raise RuntimeError("Execution kernel services are not initialized")
        return storage, knowledge_graph, web_surfer, ingestion

    async def _mission_propose(self, *, actor: ActorContext, goal: str) -> dict[str, Any]:
        if self.executive is None:
            raise RuntimeError("Executive service is not initialized")
        goal = goal.strip()
        if not goal:
            raise ValueError("goal is required")
        mission = await self.executive.create_mission(
            actor.user_id,
            goal,
            origin="agent",
            # Автор миссии — человек: в общем архиве `user_id` у всех один.
            created_by=f"agent:{actor.own_id}",
        )
        return {
            "mission_id": mission.get("id"),
            "status": mission.get("status"),
            "title": mission.get("title"),
            "task_count": mission.get("task_count"),
            "queued_for_review": mission.get("status") == "proposed",
            # Признак повтора обязан доехать до модели. Служба уже не заводит
            # вторую миссию с той же целью, но без этого поля модель отчитается
            # человеку о создании новой — то есть дедупликация починит дубли и
            # заведёт на их месте ложное подтверждение.
            "existing": bool(mission.get("existing")),
        }

    async def _mission_compensation(
        self,
        *,
        actor: ActorContext,
        mission_id: str,
        task_id: str,
        compensation: str = "",
        checkpoint: str = "",  # noqa: ARG002 — едет в заявке для человека, не для нас
    ) -> dict[str, Any]:
        """Закрыть шаг, с побочным эффектом которого человек разобрался сам.

        Заявка на компенсацию создавалась с `tool="mission_compensation"`, а такого
        инструмента в ядре не было. Человек, нажимавший «Подтвердить», получал
        «Инструмент недоступен» — отказ, который ничего не объясняет и ничего не
        меняет: заявка оставалась одобренной и неиспользованной, шаг миссии — вечно
        `uncertain`, а статус `compensated` не проставлял никто и никогда (объявлен в
        модели, разрешён схемой, ноль записей в коде).

        Откат СИСТЕМА не исполняет и здесь: текст компенсации написан для человека и
        в общем случае невыполним автоматически, а автоматический откат того, чего,
        может быть, и не было, — такой же необратимый шаг, как повтор. Поэтому
        подтверждение означает ровно одно: «я посмотрел и разобрался». Ровно это и
        сказано человеку в тексте заявки — иначе он нажимал бы кнопку, ожидая, что
        откатит Пятница.

        Возражение «тогда это вообще не инструмент, а отметка» справедливо по форме и
        неверно по сути: заявка — единственный канал, которым система спрашивает
        человека, и у ответа должен быть исполнитель. Без него цикл не замыкается.
        """
        storage, _, _, _ = self._require_services()
        mission = await run_blocking(
            storage.get_mission,
            mission_id,
            actor.user_id,
            # Владелец разбирает любую миссию, остальные — только свои. Чужая
            # отвечает тем же, чем несуществующая.
            created_by=None if actor.is_owner else f"agent:{actor.own_id}",
        )
        if not mission:
            raise ValueError("Миссия не найдена")
        tasks = await run_blocking(storage.get_mission_tasks, mission_id, actor.user_id)
        task = next((item for item in tasks if str(item.get("id")) == task_id), None)
        if task is None:
            raise ValueError("Шаг миссии не найден")
        note = (compensation or str(task.get("compensation") or "")).strip()
        updated = await run_blocking(
            storage.update_mission_task_fields,
            task_id,
            actor.user_id,
            status=TaskStatus.COMPENSATED.value,
            error="",
            result=("человек разобрался с побочным эффектом: " + (note or "без описания"))[:2000],
            completed_at=utc_now(),
        )
        if not updated:
            raise ValueError("Шаг миссии не обновился")
        return {
            "mission_id": mission_id,
            "task_id": task_id,
            "status": TaskStatus.COMPENSATED.value,
            # Отвечаем словами, а не «ok»: этот текст читает человек в подтверждении.
            "message": "Шаг закрыт как разобранный. Пятница откат не выполняла.",
        }

    def _zone(self) -> Any:
        """Часовой пояс, в котором человек называет время.

        Пустая настройка означает пояс машины: для личного экземпляра это и есть
        пояс владельца. Неизвестное имя — не повод падать посреди ответа, но и
        молча считать UTC нельзя: разница в три часа превращает «15 часов» в
        «18 часов», поэтому о подмене говорится в логе.
        """
        name = str(getattr(self.settings, "local_timezone", "") or "").strip()
        if not name:
            return datetime.now().astimezone().tzinfo or UTC
        try:
            return ZoneInfo(name)
        except Exception as exc:  # noqa: BLE001 — кривое имя пояса не должно ронять ход
            LOGGER.warning(
                "Unknown timezone in settings; falling back to machine zone (%s)",
                type(exc).__name__,
            )
            return datetime.now().astimezone().tzinfo or UTC

    async def _remind(
        self,
        *,
        actor: ActorContext,
        what: str,
        when: str,
    ) -> dict[str, Any]:
        """Поставить напоминание: событие с датой, которое орган напоминаний найдёт сам.

        Просьба «напомни мне завтра в 15:00 про совещание» — базовая для
        помощника, и до этого инструмента она не работала вовсе: замерено на
        живом прогоне — модель уходила в `memory_search`, отвечала пересказом
        найденных документов, а событие в графе не появлялось. В другой раз
        отвечала «Запомнил» и не делала ничего: обещание без действия.

        Ничего нового изобретать не пришлось — орган напоминаний каждый день
        читает события из графа и шлёт по ним сообщения. Не хватало одного:
        способа положить туда событие словами человека.
        """
        storage, knowledge_graph, _, _ = self._require_services()
        text = str(what or "").strip()
        if not text:
            return {"created": False, "reason": "не сказано, о чём напомнить"}
        # Тот же разбор времени, что у вопросов «что было 26 июля»: одна пара
        # правил на всю систему, иначе «завтра» здесь и там означало бы разное.
        local_now = datetime.now(self._zone())
        today = local_now.date()
        # Сначала будущее — «завтра», «в понедельник», «через неделю»; общий
        # разбор их не знает, он писался для вопросов о прошлом.
        ahead = _future_day(str(when or ""), today=today)
        stamp, bad = (f"{ahead}T00:00:00", None) if ahead else _moment_bounds(str(when or ""), edge="since")
        if not stamp:
            return {
                "created": False,
                "reason": f"не разобрала, когда напомнить: {bad or when!r}",
                "hint": "Скажи день прямо: «завтра», «3 августа», «в понедельник».",
            }
        occurred_at = stamp[:10]
        # Час читается из слов человека, а не из штампа: разбор будущего даёт
        # только день, а «в 15:00» человек сказал и ждёт увидеть это в тексте
        # напоминания — рассылка идёт по календарным дням.
        clock = _clock_from_text(str(when or ""))
        if not clock and len(stamp) >= 16 and stamp[11:16] != "00:00":
            clock = stamp[11:16]
        # Напоминание смотрит ВПЕРЁД, а разбор времени писался для вопросов о
        # прошлом («что было в понедельник») и берёт ближайший ПРОШЕДШИЙ день.
        # Замерено: «не дай забыть в понедельник позвонить» поставило событие на
        # 27 июля — на прошлую неделю, то есть не сработает никогда.
        planned = date.fromisoformat(occurred_at)
        if planned < today:
            if _NAMES_A_WEEKDAY.search(str(when or "")):
                # День недели без даты — человек имеет в виду ближайший будущий.
                while planned < today:
                    planned += timedelta(days=7)
                occurred_at = planned.isoformat()
            else:
                return {
                    "created": False,
                    "reason": f"названный день уже прошёл: {occurred_at}",
                    "hint": "Напоминание можно поставить только на будущее.",
                }
        if planned == today and clock:
            hour, minute = (int(part) for part in clock.split(":", 1))
            scheduled = datetime.combine(planned, time(hour, minute), tzinfo=local_now.tzinfo)
            if scheduled <= local_now:
                return {
                    "created": False,
                    "reason": f"названное время уже прошло: {planned.isoformat()} {clock}",
                    "hint": "Назови время в будущем.",
                }
        # Две записи — одно напоминание, значит одна транзакция.
        #
        # Врозь они давали настоящее половинчатое состояние: `set_event_time`
        # умеет бросить `ValueError` на неразобранной дате, и в графе оставалось
        # СОБЫТИЕ БЕЗ ВРЕМЕНИ — не напомнит никто и никогда, а человеку сказано,
        # что напоминание поставлено. Единственный боевой мутатор с двумя
        # записями вне транзакции; остальные девять давно внутри. Найдено
        # внешним разбором (Сол, 2026-08-04) при разборе «тип исключения не
        # доказывает, что эффекта не было».
        stored_what = text[:120]
        with storage.transaction():
            entity = knowledge_graph.create_entity(
                actor.own_id,
                stored_what,
                EntityType.EVENT,
                description=reminder_clock_description(clock),
                # Two reminders with the same words but different dates/times
                # are two scheduled effects, not one entity to overwrite.
                deduplicate=False,
            )
            # Автор напоминания — в источнике временной привязки. В общем архиве
            # (`FRIDAY_SHARED_ARCHIVE`) `actor.user_id` у всех один, и без этой
            # отметки орган рассылки не мог узнать, чья это просьба: замерено — она
            # уходила ХОЗЯИНУ архива, а тот, кто просил, не получал ничего.
            # События из документов остаются без отметки: у них автора нет, и
            # напоминает о них по-прежнему хозяин архива.
            knowledge_graph.set_event_time(
                actor.own_id,
                entity["id"],
                occurred_at,
                source=f"reminder:{actor.own_id}",
            )
        from friday.organs import may_push_to, resolve_chat_id

        chat_id = resolve_chat_id(storage, actor.own_id)
        delivery_scheduled = bool(
            getattr(self.settings, "reminders_enabled", False)
            and chat_id
            and may_push_to(self.settings, storage, actor.own_id, chat_id)
        )
        return {
            "created": True,
            "what": stored_what,
            "on": occurred_at,
            "at": clock,
            "requested_when": " ".join(str(when or "").split())[:120],
            "delivery_scheduled": delivery_scheduled,
            "entity_id": entity["id"],
        }

    async def _upcoming(
        self,
        *,
        actor: ActorContext,
        days: int = 7,
        since: str = "",
        until: str = "",
    ) -> dict[str, Any]:
        """Что человеку предстоит: напоминания и события с датами впереди.

        Найдено недельным прогоном 2026-08-02. На «Доброе утро! Какие планы на
        сегодня?» Пятница вызвала `what_happened` и пересказала ВЧЕРАШНЮЮ
        переписку человека: «в 00:37 смотрел статистику базы, в 02:31 спрашивал,
        как меня зовут». Инструмента, смотрящего вперёд, у неё просто не было —
        а утренний вопрос о планах для помощника руководителя основной.

        Читается та же лента, по которой рассылает орган напоминаний, и та же
        отметка автора: чужие напоминания в чужие планы не попадают.
        """
        storage, _, _, _ = self._require_services()
        from friday.storage._graph import (
            _bounded_visible_timeline_event_rows,
            _count_visible_timeline_events,
        )

        zone = self._zone()
        local_now = datetime.now(zone)
        today = local_now.date()
        exact_since = exact_until = None
        if str(since or "").strip() or str(until or "").strip():
            raw_since = str(since or "").strip()
            raw_until = str(until or "").strip()
            date_boundaries = bool(
                re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_since)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_until)
            )
            datetime_boundaries = bool(
                re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", raw_since)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", raw_until)
            )
            try:
                if date_boundaries:
                    start_day = date.fromisoformat(raw_since)
                    end_day = date.fromisoformat(raw_until)
                elif datetime_boundaries:
                    start_moment = datetime.fromisoformat(raw_since)
                    end_moment = datetime.fromisoformat(raw_until)
                    start_day = start_moment.date()
                    end_day = end_moment.date()
                    exact_since, exact_until = raw_since, raw_until
                else:
                    raise ValueError("unsupported boundary shape")
            except ValueError:
                return {
                    "understood": False,
                    "error": (
                        "Границы календаря должны быть полными ISO-датами или "
                        "локальными ISO-датами со временем."
                    ),
                    "items": [],
                }
            invalid = bool(end_day < start_day or (end_day - start_day).days >= 60 or start_day < today)
            if exact_since is not None and exact_until is not None:
                local_now_naive = local_now.replace(tzinfo=None)
                invalid = invalid or end_moment < start_moment or start_moment < local_now_naive
            if invalid:
                return {
                    "understood": False,
                    "error": ("Интервал календаря неверен, длиннее 60 дней или относится к прошлому."),
                    "items": [],
                }
        else:
            requested_days = max(1, min(int(days or 7), 60))
            start_day = today
            end_day = today + timedelta(days=requested_days - 1)
        window_days = (end_day - start_day).days + 1
        not_before = (
            local_now.replace(tzinfo=None, microsecond=0).isoformat(timespec="seconds")
            if start_day == today and exact_since is None
            else None
        )

        def read_page_and_total() -> tuple[list[dict[str, Any]], int]:
            rows = _bounded_visible_timeline_event_rows(
                storage,
                actor.user_id,
                actor.own_id,
                start=start_day.isoformat(),
                end=f"{end_day.isoformat()}T23:59:59",
                not_before=not_before,
                exact_since=exact_since,
                exact_until=exact_until,
                limit=100,
            )
            total = _count_visible_timeline_events(
                storage,
                actor.user_id,
                actor.own_id,
                start=start_day.isoformat(),
                end=f"{end_day.isoformat()}T23:59:59",
                not_before=not_before,
                exact_since=exact_since,
                exact_until=exact_until,
            )
            return rows, total

        rows, total = await run_blocking(
            _storage_read_snapshot,
            storage,
            read_page_and_total,
        )
        items = []
        for row in rows:
            source = str(row.get("source") or "")
            # Напоминание принадлежит тому, кто его поставил; событие из
            # документа — общее. То же правило, что у органа рассылки.
            if source.startswith("reminder:") and source[len("reminder:") :] != actor.own_id:
                continue
            occurred_at = str(row.get("occurred_at") or "")
            clock = reminder_clock(row) or (occurred_at[11:16] if len(occurred_at) >= 16 else "")
            when = reminder_when_text(row, today)
            items.append(
                {
                    # Forty long reminder names otherwise exceed the shared LLM
                    # payload boundary and get cut in the middle of JSON.  The
                    # full value remains in storage; this is only the bounded
                    # reasoning projection, matching timeline event excerpts.
                    "what": str(row.get("name") or "")[:200],
                    "on": occurred_at,
                    "when": when,
                    "at": clock,
                    "mine": source.startswith("reminder:"),
                }
            )
        # `total` — сколько запланировано ВСЕГО в этом окне, `shown` — сколько
        # попало в ответ. Раньше здесь стояла длина собственной выборки (потолок
        # 100, показ 40), то есть размер запроса выдавался за содержимое
        # календаря: «на неделю запланировано 100» при потолке ровно 100.
        shown = items[:40]
        return {
            "understood": True,
            "days": window_days,
            "asked_about": {
                "since": exact_since or start_day.isoformat(),
                "until": exact_until or end_day.isoformat(),
                "timezone": str(zone),
            },
            "total": total,
            "shown": len(shown),
            "items": shown,
            "note": (
                ""
                if total
                else f"В интервале {start_day.isoformat()} — {end_day.isoformat()} ничего не запланировано."
            ),
        }

    async def _what_happened(
        self,
        *,
        actor: ActorContext,
        since: str,
        until: str = "",
        limit: int = 40,
    ) -> dict[str, Any]:
        """Что происходило в названный момент или промежуток.

        Отдельный инструмент, а не фильтр к поиску: на вопрос «что было 26 июля в
        15 часов» поиск ищет СЛОВА, а спрашивают о МОМЕНТЕ. По словам «26 июля» и
        «15 часов» найдутся документы, где эти даты УПОМЯНУТЫ, — совсем не то,
        что появилось тогда.

        `until` необязателен: без него берётся тот же промежуток, что и `since`
        («26 июля в 15 часов» — это час целиком, «26 июля» — день целиком).
        """
        storage = self.storage
        if storage is None:
            raise RuntimeError("Execution kernel storage is not initialized")
        normalized_start = _normalized_local_timestamp(since)
        normalized_end = _normalized_local_timestamp(until) if until else None
        start_local: str | None
        end_local: str | None
        start_bad: str | None
        end_bad: str | None
        if normalized_start and normalized_end:
            start_local, end_local = normalized_start, normalized_end
            start_bad = end_bad = None
            start_value = datetime.fromisoformat(start_local)
            end_value = datetime.fromisoformat(end_local)
            duration = end_value - start_value
            if duration < timedelta(0) or (end_value.date() - start_value.date()).days >= 60:
                start_bad = "интервал перевёрнут или длиннее 60 дней"
        else:
            start_local, start_bad = _moment_bounds(since, edge="since")
            # Без явного конца названное время означает промежуток вокруг себя, а не
            # мгновение: «в 15:00» человек спрашивает про пятнадцатый час, и ответ
            # «за 15:00:00–15:00:59» был бы пустым почти всегда.
            end_local, end_bad = _moment_bounds(until or since, edge="until", widen=not until)
        if start_bad or end_bad or not start_local or not end_local:
            # Непонятая граница — отказ, а не «показать всё»: молча снятый фильтр
            # выдаёт чужое время за спрошенное.
            return {
                "understood": False,
                "error": f"Не понял момент: {start_bad or end_bad!r}. "
                "Примеры: «2026-07-26 15:00», «26 июля 2026», «2026-07-26».",
                "events": [],
            }
        start_value = datetime.fromisoformat(start_local)
        end_value = datetime.fromisoformat(end_local)
        if end_value < start_value or (end_value.date() - start_value.date()).days >= 60:
            return {
                "understood": False,
                "error": "Интервал ленты перевёрнут или длиннее 60 дней.",
                "events": [],
            }
        zone = self._zone()
        local_now = datetime.now(zone).replace(tzinfo=None)
        if start_value > local_now or end_value.date() > local_now.date():
            return {
                "understood": False,
                "error": "Лента прошлого не принимает границы из будущего.",
                "events": [],
            }
        if end_value > local_now:
            # A day/week/month ending today denotes its elapsed prefix.  Clip
            # both the storage boundary and the echoed contract boundary so a
            # caller can verify exactly what was checked; never read scheduled
            # rows from the unelapsed tail as if they had happened already.
            end_value = local_now.replace(microsecond=0)
            end_local = end_value.isoformat(timespec="seconds")
        if end_value < start_value:
            return {
                "understood": False,
                "error": "Лента прошлого не принимает границы из будущего.",
                "events": [],
            }
        since_utc = datetime.fromisoformat(start_local).replace(tzinfo=zone).astimezone(UTC)
        until_utc = datetime.fromisoformat(end_local).replace(tzinfo=zone).astimezone(UTC)

        # Переписка — ЛИЧНАЯ, документы — общие, и это две разные границы.
        # До правки обе шли одним `actor.user_id`: в общем архиве это арендатор, и
        # любой участник, спросивший «что было вчера», получал реплики ВЛАДЕЛЬЦА
        # дословно — с ролью и заголовком разговора, — а своих не видел ни одной.
        # Воспроизведено на изолированном стенде; тот же класс уже чинили в
        # `_message_search` и `_upcoming`.
        def read_page_and_total() -> tuple[list[dict[str, Any]], dict[str, int]]:
            events = storage.what_happened(
                actor.user_id,
                person_id=actor.own_id,
                since=since_utc.isoformat(),
                until=until_utc.isoformat(),
                limit=max(1, min(int(limit), 200)),
            )
            totals = storage.count_what_happened(
                actor.user_id,
                person_id=actor.own_id,
                since=since_utc.isoformat(),
                until=until_utc.isoformat(),
            )
            return events, totals

        events, totals = await run_blocking(
            _storage_read_snapshot,
            storage,
            read_page_and_total,
        )
        for event in events:
            # Человеку — его время, а не UTC: иначе ответ на «в 15 часов» будет
            # называть 12:00 и выглядеть как ошибка.
            try:
                event["at_local"] = (
                    datetime.fromisoformat(str(event["at"])).astimezone(zone).strftime("%Y-%m-%d %H:%M")
                )
            except ValueError:
                event["at_local"] = str(event["at"])
        events = [_timeline_event_for_llm(event) for event in events]
        total_events = int(totals.get("total", 0) or 0)
        return {
            "understood": True,
            "asked_about": {"since": start_local, "until": end_local, "timezone": str(zone)},
            "total": totals,
            "shown": len(events),
            "events": events,
            "coverage": {
                "complete": len(events) == total_events,
                "strategy": "complete" if len(events) == total_events else "uniform_interval_sample",
                "includes_latest": bool(events) or total_events == 0,
            },
        }

    async def _memory_search(
        self,
        *,
        actor: ActorContext,
        query: str,
        limit: int = 10,
        since: str | None = None,
        until: str | None = None,
        as_of: str = "",
        known_at: str = "",
    ) -> dict[str, Any]:
        """Поиск по своему архиву — ВЫДЕРЖКАМИ, а не телами документов.

        Инструмент отдавал строки целиком (`SELECT k.*`), и результат обрезался на
        12 000 знаках в `to_llm_message`. Замерено на архиве владельца: средняя длина
        документа 16 565 знаков, то есть ОДИН средний документ переполняет весь бюджет;
        231 документ длиннее самого бюджета, самый длинный — 1.3 млн знаков. На
        реальных запросах до модели доходил один результат из десяти, а поскольку
        `results` шёл в ответе раньше `count`, обрезка съедала и счётчик — модель не
        видела даже, сколько было найдено.

        И обрезалась ГОЛОВА документа, а не совпавший фрагмент. Для контекстного пути
        это чинили отдельно («Quote the passage that matched, not the top of the
        document»); до инструмента памяти починка не дошла, поэтому агент, ища САМ,
        снова получал титульную страницу.

        Теперь: проекция полей плюс выдержка вокруг совпадения. Счётчик — ПЕРВЫМ
        ключом, чтобы он пережил любую обрезку.
        """
        if not isinstance(as_of, str):
            raise ValueError("as_of must be a string")
        if not isinstance(known_at, str):
            raise ValueError("known_at must be a string")
        # Требуется ТОЛЬКО хранилище: поиск по своему архиву не зависит ни от веба, ни
        # от конвейера приёма, и общий `_require_services` отказывал бы там, где
        # отказывать не за что.
        storage = self.storage
        if storage is None:
            raise RuntimeError("Execution kernel storage is not initialized")
        limit = max(1, min(int(limit), 50))
        requested_known_at = known_at.strip()
        normalized_known_at = normalize_known_at(requested_known_at) if requested_known_at else ""
        requested_as_of = " ".join(as_of.split())
        parsed_as_of = iso_date(requested_as_of) if requested_as_of else None
        if requested_as_of and not parsed_as_of:
            # This refusal happens before either the hybrid or SQLite search.  A
            # malformed historical date must not silently become a current answer.
            return {
                "count": 0,
                "query": query,
                "as_of": "",
                "empty_because": "as_of_unparsed",
                "detail": (
                    f"не понял дату снимка: {requested_as_of}. Ожидается календарная дата ГГГГ-ММ-ДД."
                ),
                "results": [],
            }
        normalized_as_of = parsed_as_of or ""
        temporal_requested = bool(normalized_as_of or normalized_known_at)
        # Границы периода нормализуются ДО поиска. Непонятая граница — это отказ, а не
        # «искать по всему архиву»: молча снятый фильтр выдаёт документы чужого периода
        # как документы запрошенного, и человек об этом не узнаёт. См. `_window_bound`.
        since, since_bad = _window_bound(since, edge="since")
        until, until_bad = _window_bound(until, edge="until")
        if since_bad or until_bad:
            return {
                "count": 0,
                "query": query,
                "empty_because": "date_window_unparsed",
                "detail": (
                    f"не понял период: {since_bad or until_bad}. Ожидается ГГГГ-ММ-ДД, ГГГГ-ММ или ГГГГ."
                ),
                "results": [],
            }
        history_status: dict[str, Any] = {}
        if normalized_known_at:
            # The timestamp parser above is deliberately pure and runs before this
            # first database read. Valid-time alone needs no relation-history read.
            history_status = _validated_known_at_preflight(
                await run_blocking(
                    storage.relation_history_status,
                    actor.user_id,
                    known_at=normalized_known_at,
                ),
                known_at=normalized_known_at,
            )
        # Гибридный поиск, если он выдан: у инструмента был свой, на FTS-префиксе и
        # LIKE, без эмбеддингов и без морфологии. Замерено на живой базе: «поставка»
        # находит 0 документов, «поставк» — 2; «отчет» — 13, «отчёт» — 3. То есть
        # слово в именительном падеже — ровно так его напишет модель, переформулируя
        # вопрос, — давало честное «ничего не нашлось» при существующих документах, и
        # 1537 честных векторов на этом пути не участвовали.
        rows: list[dict[str, Any]]
        found: Mapping[str, Any] | None = None
        dropped = 0
        # Объявляется ДО ветвления: без поиска (ядро собирают и без него — тесты, CLI)
        # ветка `else` не задаёт стратегию вовсе, и чтение ниже роняло весь инструмент.
        strategy: Any = None
        if self.searcher is not None:
            raw_found = await self.searcher.search(
                actor.user_id,
                query,
                limit=limit,
                since=since,
                until=until,
                as_of=normalized_as_of,
                known_at=normalized_known_at,
                kg=self.kg,
                # Ordinary archive recall measured worse with graph expansion.
                # A named historical snapshot or relational-language query is the
                # explicit, measured class in which the graph is part of the ask.
                graph_expansion=bool(normalized_as_of or normalized_known_at or is_relational_query(query)),
            )
            if not isinstance(raw_found, Mapping):
                raise ValueError("memory search response is not a mapping")
            found = raw_found
            if temporal_requested:
                _assert_snapshot_as_of(
                    found,
                    as_of=normalized_as_of,
                    label="memory search envelope",
                )
                if normalized_known_at:
                    _assert_temporal_snapshot_agrees(
                        found,
                        history_status,
                        label="memory search envelope",
                    )
                else:
                    _assert_valid_time_basis(found, label="memory search envelope")
            raw_rows = found.get("results")
            rows = (
                [dict(row) for row in raw_rows if isinstance(row, Mapping)]
                if isinstance(raw_rows, list)
                else []
            )
            strategy = found.get("strategy")
            if isinstance(strategy, dict):
                try:
                    dropped = int(strategy.get("rerank_dropped") or 0)
                except (TypeError, ValueError):
                    dropped = 0
        else:
            rows = storage.search_knowledge(actor.user_id, query, limit=limit)
            if since or until:
                # Запасной путь (ядро без поиска — тесты, CLI) игнорировал период
                # ЦЕЛИКОМ: тот же молчаливый обман, что и неразобранная граница, —
                # человек просил период, получал весь архив и не узнавал об этом.
                # Окно считается тем же предикатом, что и в основном пути.
                window = storage.knowledge_ids_in_window(actor.user_id, since=since, until=until)
                if window is not None:
                    rows = [row for row in rows if str(row.get("id") or "") in window]
        results = [
            {
                "id": str(row.get("id") or ""),
                "title": str(row.get("title") or "Без названия")[:200],
                "kind": str(row.get("knowledge_kind") or "note"),
                "updated_at": row.get("updated_at"),
                # Выдержка вокруг совпадения, а не начало документа.
                "excerpt": best_snippet(query, str(row.get("content") or ""), max_chars=_TOOL_EXCERPT_CHARS),
            }
            for row in rows
        ]
        payload: dict[str, Any] = {
            "count": len(results),
            "query": query,
            "as_of": normalized_as_of,
            "known_at": normalized_known_at,
        }
        if normalized_known_at:
            payload.update(history_status)
        elif normalized_as_of:
            payload["temporal_basis"] = "valid_time"
        # «Показано 10» и «в архиве ровно 10» — разные утверждения, и модель без
        # этой строки делает второе из первого. Замерено на живом корпусе: на
        # вопрос «кто из Уфы» она называла ОДНОГО человека как полный ответ, тогда
        # как Уфу упоминают 29 документов. Число стоит рядом с `count` и ДО
        # `results` — ответ инструмента режется по хвосту, и всё, что должно
        # пережить обрезку, стоит первым.
        matched = found.get("matched_at_least") if isinstance(found, Mapping) else None
        if isinstance(matched, int) and matched > len(results):
            payload["matched_at_least"] = matched
            payload["shown"] = len(results)
        if isinstance(strategy, dict) and strategy.get("date_window_empty"):
            # «В этот период ничего нет» и «в архиве нет ничего по теме» — разные
            # ответы человеку, и без этой строки модель выдаёт второй в обоих случаях.
            payload["empty_because"] = "date_window"
        if dropped:
            # «В архиве этого нет» и «нашлось двадцать, ни одно не отвечает» — разные
            # ответы, и модель без этого числа выдаёт первый в обоих случаях. Порог
            # отбирает молча, и молчание здесь означало бы, что архив пуст по теме,
            # хотя похожее в нём есть и человек его помнит.
            #
            # Стоит ДО results по той же причине, по которой count стоит первым:
            # `to_llm_message` режет длинный ответ по хвосту, и счётчик, стоящий
            # после выдержек, до модели не доживал.
            payload["filtered_out"] = dropped
        if isinstance(found, Mapping) and "graph_context" in found:
            raw_graph = found.get("graph_context")
            if temporal_requested:
                graph_source = _assert_snapshot_as_of(
                    raw_graph,
                    as_of=normalized_as_of,
                    label="memory graph_context",
                )
                if normalized_known_at:
                    _assert_temporal_snapshot_agrees(
                        graph_source,
                        history_status,
                        label="memory graph_context",
                    )
                else:
                    _assert_valid_time_basis(graph_source, label="memory graph_context")
            elif not isinstance(raw_graph, Mapping):
                # Preserve the legacy/current behavior: an unusable optional graph
                # projection becomes an empty public snapshot. Temporal calls take
                # the strict branch above and may never degrade this way.
                graph_source = {}
            else:
                graph_source = raw_graph
            payload["graph_context"] = _memory_graph_context_for_llm(
                graph_source,
                query=str(found.get("query") or query),
                as_of=normalized_as_of,
                known_at=normalized_known_at,
            )
        elif temporal_requested and self.kg is not None:
            # A legacy/no-searcher kernel still owns the graph service.  Perform one
            # explicitly bounded traversal and require it to echo every boundary;
            # missing provenance cannot be overlaid onto possibly-current paths.
            raw_graph_source = await run_blocking(
                self.kg.context_for_query,
                actor.user_id,
                query,
                as_of=normalized_as_of,
                known_at=normalized_known_at,
            )
            graph_source = _assert_snapshot_as_of(
                raw_graph_source,
                as_of=normalized_as_of,
                label="KG fallback snapshot",
            )
            if normalized_known_at:
                _assert_temporal_snapshot_agrees(
                    graph_source,
                    history_status,
                    label="KG fallback snapshot",
                )
            else:
                _assert_valid_time_basis(graph_source, label="KG fallback snapshot")
            payload["graph_context"] = _memory_graph_context_for_llm(
                graph_source,
                query=query,
                as_of=normalized_as_of,
                known_at=normalized_known_at,
            )
        elif temporal_requested:
            raise ValueError("historical graph snapshot is unavailable")
        if normalized_known_at:
            confirmed_status = _validated_known_at_preflight(
                await run_blocking(
                    storage.relation_history_status,
                    actor.user_id,
                    known_at=normalized_known_at,
                ),
                known_at=normalized_known_at,
            )
            if confirmed_status != history_status:
                raise ValueError("relation-history status changed during memory_search")
        payload["results"] = results
        return payload

    async def _message_search(
        self,
        *,
        actor: ActorContext,
        query: str,
        limit: int = 10,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Search the caller's own chat history — not the knowledge base.

        ``memory_search`` only sees confirmed knowledge_objects. People ask
        «что я спрашивал про X» and expect the conversation, so this tool
        hits ``messages`` (FTS).

        Ищется по `own_id`, а не по арендатору. Общими владелец сделал документы
        и записи — не разговоры. В общем архиве (`FRIDAY_SHARED_ARCHIVE`)
        `actor.user_id` у всех один, и поиск по нему означал бы, что любой
        участник читает переписку всех остальных словом из неё. Найдено тотальным
        аудитом; та же ошибка уже ловилась в привязке каналов и в напоминаниях.
        """
        storage = self.storage
        if storage is None:
            raise RuntimeError("Execution kernel storage is not initialized")
        limit = max(1, min(int(limit), 50))
        conv = " ".join(str(conversation_id or "").split()).strip() or None
        rows = storage.search_messages(
            actor.own_id,
            query,
            limit=limit,
            conversation_id=conv,
        )
        results = [
            {
                "id": str(row.get("id") or ""),
                "conversation_id": str(row.get("conversation_id") or ""),
                "role": str(row.get("role") or ""),
                "created_at": row.get("created_at"),
                "excerpt": best_snippet(query, str(row.get("content") or ""), max_chars=_TOOL_EXCERPT_CHARS),
            }
            for row in rows
        ]
        return {"count": len(results), "query": query, "results": results}

    async def _memory_save(
        self,
        *,
        actor: ActorContext,
        content: str,
        title: str = "",
        tags: list[str] | None = None,
        importance: float = 0.5,
    ) -> dict[str, Any]:
        _, _, _, ingestion = self._require_services()
        body = f"{title.strip()}\n\n{content.strip()}" if title.strip() else content.strip()
        if not body:
            raise ValueError("content is required")
        parsed_importance = float(importance)
        if not 0.0 <= parsed_importance <= 1.0:
            raise ValueError("importance must be between 0 and 1")
        return await ingestion.queue_agent_candidate(
            actor.user_id,
            body,
            source_ref=new_id("toolref"),
            candidate_type="memory",
            metadata={
                "tool": "memory_save",
                # Кто попросил сохранить — ЧЕЛОВЕК. В общем архиве арендатор у
                # всех один, и запись «кто это добавил» стала бы одинаковой у
                # всех участников: владелец разбирает входящие и должен видеть
                # автора.
                "requested_by": actor.own_id,
                "review_boundary": "inbox",
            },
            suggestion_overrides={
                "title": title.strip(),
                "tags": tags or [],
                "importance": parsed_importance,
            },
        )

    async def _web_search(
        self,
        *,
        actor: ActorContext,
        query: str,
        max_results: int = 5,
        site: str = "",
        freshness: str = "",
        include_domains: list[str] | tuple[str, ...] = (),
        exclude_domains: list[str] | tuple[str, ...] = (),
        lang: str = "",
        region: str = "",
    ) -> dict[str, Any]:
        _, _, web, _ = self._require_services()
        raw_site = site
        raw_include_domains = include_domains
        # Validate before quota accounting and before any provider can see the
        # values.  Error messages are closed strings and never echo a domain.
        site, include_domains, exclude_domains, freshness, lang, region = normalize_search_filters(
            site=site,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            freshness=freshness,
            lang=lang,
            region=region,
        )
        refusal = await self._what_must_not_leave(query, actor)
        if refusal:
            return refusal
        outbound_domain_forms: list[str] = []
        if site:
            outbound_domain_forms.extend((raw_site, site))
        else:
            for raw_domain, canonical_domain in zip(
                raw_include_domains,
                include_domains,
                strict=True,
            ):
                outbound_domain_forms.extend((raw_domain, canonical_domain))
        checked_domain_forms: set[str] = set()
        for domain in outbound_domain_forms:
            if domain in checked_domain_forms:
                continue
            checked_domain_forms.add(domain)
            # Check both the human spelling and outbound punycode.  Checking
            # only canonical IDNA would hide a private person's name from the
            # local privacy classifier before the provider sees its encoding.
            refusal = await self._what_must_not_leave(domain, actor)
            if refusal:
                return refusal
        exhausted = self._web_quota_refusal(actor)
        if exhausted:
            return {**exhausted, "query": query}
        query = query[:_MAX_OUTBOUND_QUERY_CHARS]
        search_options: dict[str, Any] = {
            "max_results": max(1, min(int(max_results), 10)),
        }
        # Keeping absent filters absent preserves compatibility with local test
        # doubles and with adapters written against the original search method.
        if site:
            search_options["site"] = site
        if freshness:
            search_options["freshness"] = freshness
        if include_domains:
            search_options["include_domains"] = list(include_domains)
        if exclude_domains:
            # This crosses only the in-process WebSurfer boundary.  Provider
            # adapters deliberately never serialise deny-list values.
            search_options["exclude_domains"] = list(exclude_domains)
        if lang:
            search_options["lang"] = lang
        if region:
            search_options["region"] = region
        try:
            results = await web.search(query, **search_options)
        except SearchFilterUnavailableError as capability_failure:
            LOGGER.warning(
                "Web search filter unavailable (%d chars): %s",
                len(query),
                capability_failure.filter_name,
            )
            return {
                "query": query,
                "results": [],
                "search_failed": True,
                "unsupported_filters": list(capability_failure.filter_names),
                "error": "Доступные поисковые системы не умеют применить все запрошенные фильтры.",
                "note": "Нефильтрованная выдача не запрашивалась и не использовалась.",
            }
        except AllProvidersRefusedError as exc:
            # Отказ поисковиков — не факт об интернете. Модель обязана увидеть
            # разницу, иначе она сообщит человеку «ничего не нашлось» о запросе,
            # который никто не искал, либо ответит из своей памяти как из архива.
            # Запрос в журнал НЕ пишется. `docs/SECURITY.md` обещает, что поисковые
            # строки туда не попадают: они персональные данные (ФИО, номер
            # договора, тема личного дела), а журнал переживает и удаление знания,
            # и `purge` — чистится только база, не journald.
            #
            # Уровень WARNING делает это хуже, а не лучше: строка остаётся видимой
            # даже когда владелец поднял порог логирования, чтобы личное туда не
            # текло. Для диагностики хватает длины и класса отказа.
            LOGGER.warning("Web search refused (%d chars): %s", len(query), type(exc).__name__)
            return {
                "query": query,
                "results": [],
                "search_failed": True,
                "error": "Поисковые системы не ответили — это сбой доступа, а не отсутствие результатов.",
                # Факт, а не поручение. Здесь стояло «Скажи человеку… и НЕ
                # отвечай…» под ключом `instruction` — то есть приказ себе,
                # положенный в данные инструмента. Модель не отличает одно от
                # другого, и такие строки уезжают человеку целиком.
                #
                # Содержание сохранено ровно то же: в этом результате сведений из
                # интернета нет вовсе, и всё, что похоже на найденное, найденным
                # не является.
                "note": "Сведений из интернета в этом результате нет: ни одна выдача не получена.",
            }
        response: dict[str, Any] = {
            "query": query,
            "results": [item.to_dict() for item in results],
        }
        if site or include_domains or exclude_domains:
            requested_results = int(search_options["max_results"])
            returned_results = len(results)
            response.update(
                {
                    "requested_results": requested_results,
                    "returned_results": returned_results,
                    "underfilled": returned_results < requested_results,
                }
            )
            if returned_results < requested_results:
                response["note"] = (
                    "Подходящих результатов меньше запрошенного; доменные ограничения не ослаблялись."
                )
        return response

    async def _web_fetch(self, *, actor: ActorContext, url: str, query: str = "") -> dict[str, Any]:
        _, _, web, _ = self._require_services()
        exhausted = self._web_quota_refusal(actor)
        if exhausted:
            return {**exhausted, "url": url, "text": "", "text_length": 0}
        # `query` необязателен и означает «что искать на странице»: с ним модель
        # получает кусок вокруг совпадения, без него — начало страницы.
        return (await web.fetch(url)).to_dict(query=query)

    async def _web_research(self, *, actor: ActorContext, query: str, max_sources: int = 3) -> dict[str, Any]:
        _, _, web, _ = self._require_services()
        refusal = await self._what_must_not_leave(query, actor)
        if refusal:
            return refusal
        exhausted = self._web_quota_refusal(actor)
        if exhausted:
            return {**exhausted, "query": query, "sources": []}
        query = query[:_MAX_OUTBOUND_QUERY_CHARS]
        report = await web.research(query, max_sources=max(1, min(int(max_sources), 8)))
        captured = await self._capture_web_sources(actor, query, report)
        return {**report, "captured": captured} if captured else report

    def _web_quota_refusal(self, actor: ActorContext) -> dict[str, Any] | None:
        """Не исчерпан ли суточный выход в интернет. `None` — можно идти.

        Ворота стоят на ВСЕХ трёх дорогах (`web_search`, `web_fetch`,
        `web_research`), а не на одной: `_what_must_not_leave` зовут только две
        из них, и квота на том же месте охраняла бы через раз.

        Размер взят замером на живом архиве: пик 135 вызовов на человека за
        сутки, медиана по человеко-дням 76. Потолок 400 — тройной запас над
        настоящим пиком, потому что защита нужна не от человека, а от цикла.

        Отказ НАЗЫВАЕТ причину и число. Молчаливое «ничего не нашлось» модель
        пересказала бы человеку как факт об интернете — тот же класс, что чужой
        отказ поисковика, выданный за пустую выдачу.
        """

        storage, _, _, _ = self._require_services()
        settings = self.settings
        if settings is None:  # pragma: no cover - ядро без настроек не работает
            return None
        limit = int(getattr(settings, "web_daily_quota", 0) or 0)
        if limit <= 0:
            return None
        # Сутки — местные для ЧЕЛОВЕКА, а не UTC: иначе счёт обнуляется среди
        # его рабочего дня. Тот же выбор, что у ночной сводки.
        day = local_now(settings).date().isoformat()
        used = storage.bump_daily_counter("web", actor.own_id, day)
        if used <= limit:
            return None
        LOGGER.warning("web quota exhausted: %d > %d", used, limit)
        return {
            "results": [],
            "quota_exhausted": True,
            "error": (
                f"Суточный лимит обращений в интернет исчерпан: {limit} за сутки. "
                "Счёт обнулится в полночь по вашему времени."
            ),
            "note": "Сведений из интернета в этом результате нет: наружу не ходили вовсе.",
        }

    async def _what_must_not_leave(self, query: str, actor: ActorContext) -> dict[str, Any] | None:
        """Не уходит ли наружу имя человека из архива. `None` — можно искать.

        Ворота стояли на ОДНОЙ дороге — предвыборке в `agent_runtime`, — а модель
        зовёт `web_search` и `web_research` напрямую. Замерено на стенде: запрос
        «что известно про Хасанова Рустама Маратовича?» уходил в поисковик
        целиком, вместе с фамилией. Проверка при этом существовала и работала —
        просто мимо неё вела вторая дорога.

        Здесь она стоит в самом инструменте, то есть в единственном месте, через
        которое наружу идёт всё. Ворота на одной дороге не охраняют ничего.

        Решает СТРУКТУРА, а не модель: слово из запроса совпало с именем человека
        в этом графе — значит вопрос про своего, и наружу ему не надо. Цена
        ошибки несимметрична: лишний отказ человек увидит сразу и переспросит, а
        ушедшую фамилию не вернуть — в журнале останется только хеш запроса.
        """
        storage, _, _, _ = self._require_services()
        if not hasattr(storage, "people_whose_name_starts_with"):
            LOGGER.warning("Outbound privacy guard is unavailable; web request refused")
            return self._outbound_privacy_guard_refusal()
        words = [
            word.strip(".,!?…«»\"'()[]:;")
            for word in str(query or "").split()
            if len(word.strip(".,!?…«»\"'()[]:;")) >= 4
        ]
        # Сравнивается ОСНОВА, а не слово целиком: у русских фамилий меняется
        # окончание. Замерено — прежняя проверка через `search_entities` находила
        # «Хасанов» и не находила «Хасанова», «Хасанову», «Хасановым»; спрашивают
        # же обычно в косвенном падеже. Ворота узнавали одну форму из шести.
        stems = [item[:5] for item in words if item.casefold() not in _NOT_A_NAME_OUTBOUND][:6]
        if not stems:
            return None
        try:
            found = await run_blocking(storage.people_whose_name_starts_with, actor.user_id, stems)
        except Exception as exc:  # noqa: BLE001 — проверка не должна ронять ход
            LOGGER.warning(
                "Could not check the graph before a web search (%s)",
                type(exc).__name__,
            )
            return self._outbound_privacy_guard_refusal()
        if found:
            return {
                "query": "",
                "results": [],
                "refused": True,
                "reason": (
                    "В запросе имя человека из архива — наружу такой запрос не "
                    "уходит. Ответ по нему ищется в своих материалах."
                ),
            }
        return None

    @staticmethod
    def _outbound_privacy_guard_refusal() -> dict[str, Any]:
        """Fail closed when the archive privacy check cannot prove a query safe."""

        return {
            "query": "",
            "results": [],
            "refused": True,
            "reason": (
                "Не удалось безопасно проверить запрос по архиву, поэтому наружу "
                "он не отправлен. Попробуйте ещё раз позже."
            ),
        }

    async def _capture_web_sources(
        self, actor: ActorContext, query: str, report: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Найденное в интернете — такой же материал, как присланный файл.

        Требование владельца (2026-08-01): результаты поиска Пятницы должны быть
        полноправным участником общего конвейера — связываться с людьми, тегами и
        сущностями, обрабатываться как документы. Раньше страница жила ровно один
        ход: показали модели и забыли, и на завтрашний вопрос про то же самое всё
        искалось заново.

        Путь тот же, что у `POST /api/ingest/url`: страница становится Raw Object
        и идёт через Inbox, а не в знания молча — `force_review=True`. Иначе
        каждый гуглинг тихо дописывал бы в архив содержимое чужих сайтов.

        Права проверяются честно: без `knowledge.create` поиск работает, а запись
        не делается — искать и запоминать это разные разрешения.
        """
        sources = report.get("sources")
        if not isinstance(sources, list) or not sources:
            return []
        if not (self.authorization and self.authorization.authorize(actor, "knowledge.create").allowed):
            return []
        _, _, _, ingestion = self._require_services()
        captured: list[dict[str, Any]] = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            url = str(source.get("url") or "").strip()
            text = str(source.get("text") or "").strip()
            # Пустая страница знанием не становится: сохранять нечего, а строка
            # в Inbox с одним заголовком — работа для человека на ровном месте.
            if not url or len(text) < _WEB_CAPTURE_MIN_CHARS:
                continue
            title = str(source.get("title") or source.get("search_title") or url)
            # Ключ несёт и адрес, и содержимое. Страница живая: курс ЦБ, прогноз
            # погоды, лента новостей меняются между двумя чтениями, и адрес,
            # взятый ключом в одиночку, конфликтовал сам с собой — замерено на
            # живом экземпляре, пять срывов за сутки, каждый со стеком в журнале
            # и потерянной страницей. Неизменная страница по-прежнему не
            # задваивается: у неё тот же хеш.
            fingerprint = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
            try:
                outcome = await ingestion.ingest_text(
                    actor.user_id,
                    text,
                    source="web",
                    source_ref=f"{url}#{fingerprint}",
                    force_review=True,
                    metadata={
                        "url": url,
                        "title": title,
                        "content_source": "web_research",
                        # Поиск выполняет конкретный человек даже тогда, когда
                        # найденная страница сохраняется в общий tenant.
                        "uploaded_by": actor.own_id,
                        # Запрос — часть провенанса: по нему видно, зачем эта
                        # страница вообще попала в архив.
                        "search_query": query,
                        **({"content_truncated": True} if source.get("truncated") else {}),
                    },
                )
            except Exception as exc:  # noqa: BLE001 — сохранение не должно ронять ответ
                LOGGER.warning("Failed to capture web source (%s)", type(exc).__name__)
                continue
            captured.append(
                {
                    "url": url,
                    "title": title,
                    "raw_object_id": outcome.get("raw_object_id"),
                    "inbox_id": outcome.get("inbox_id"),
                    "duplicate": bool(outcome.get("duplicate")),
                }
            )
        return captured

    async def _entity_lookup(
        self,
        *,
        actor: ActorContext,
        name: str,
        as_of: str = "",
        known_at: str = "",
    ) -> dict[str, Any]:
        """Карточка объекта on valid-time and optional transaction-time axes.

        Без этого параметра вопрос «кто командовал батальоном в 2024» отвечался
        сегодняшней картиной: связи, отменённые с тех пор, из неё уже вычеркнуты,
        а связи, подтверждённые позже, в неё уже попали. Ответ выглядел уверенно
        и относился не к тому году.
        """

        if not isinstance(as_of, str):
            raise ValueError("as_of must be a string")
        if not isinstance(known_at, str):
            raise ValueError("known_at must be a string")
        requested_as_of = as_of.strip()
        if requested_as_of:
            # Local import avoids turning execution_kernel -> knowledge_graph into
            # a module-initialization cycle while still sharing calendar semantics.
            from friday.knowledge_graph import normalize_event_date

            normalized_as_of = normalize_event_date(requested_as_of)[0]
        else:
            normalized_as_of = ""
        requested_known_at = known_at.strip()
        normalized_known_at = normalize_known_at(requested_known_at) if requested_known_at else ""
        temporal_requested = bool(normalized_as_of or normalized_known_at)

        # Both temporal axes are normalized before `_require_services` and before
        # the first entity/status read. A malformed boundary can never reveal even
        # whether the named entity exists.
        storage, kg, _, _ = self._require_services()
        history_status: dict[str, Any] = {}
        if normalized_known_at:
            # Validate strict syntax before the first database read, then validate
            # completeness/identity before even revealing whether the entity exists.
            history_status = _validated_known_at_preflight(
                await run_blocking(
                    storage.relation_history_status,
                    actor.user_id,
                    known_at=normalized_known_at,
                ),
                known_at=normalized_known_at,
            )
        # Через поток, как и HTTP-двойник этой же карточки: профиль на широкой
        # сущности — несколько SQL по 22 тысячам связей. Маршрут перевели на
        # `run_blocking`, а этот путь забыли — и он ХУЖЕ, потому что инструмент
        # зовётся внутри агентского цикла, где ждут все остальные разговоры.
        entity = await run_blocking(kg.find_entity, actor.user_id, name)
        if not entity:
            if not temporal_requested:
                return {"found": False, "entity": None}
            if normalized_known_at:
                confirmed_status = _validated_known_at_preflight(
                    await run_blocking(
                        storage.relation_history_status,
                        actor.user_id,
                        known_at=normalized_known_at,
                    ),
                    known_at=normalized_known_at,
                )
                if confirmed_status != history_status:
                    raise ValueError("relation-history status changed during entity_lookup")
            return {
                "found": False,
                "entity": None,
                "as_of": normalized_as_of,
                **(
                    history_status
                    if normalized_known_at
                    else {"known_at": "", "temporal_basis": "valid_time"}
                ),
            }
        profile = await run_blocking(
            kg.entity_profile,
            entity["id"],
            actor.user_id,
            knowledge_limit=10,
            relation_limit=_ENTITY_LOOKUP_RELATION_CAP,
        )
        if not isinstance(profile, Mapping):
            raise ValueError("entity profile is not a mapping")
        public_entity = {
            key: str(entity.get(key) or "")[:limit]
            for key, limit in (
                ("id", 160),
                ("name", 240),
                ("entity_type", 80),
                ("description", 500),
            )
            if entity.get(key) is not None
        }
        profile_fields = {
            "profile",
            "profile_provenance",
            "knowledge_objects_total",
            "pending_relations_count",
            "event_time",
            "edits",
            "knowledge_objects",
        }
        safe_profile = {key: profile[key] for key in profile_fields if key in profile}
        raw_current_relations = profile.get("relations")
        current_relations = (
            [edge for edge in raw_current_relations if isinstance(edge, Mapping)]
            if isinstance(raw_current_relations, list)
            else []
        )
        try:
            current_relations_matched = max(
                len(current_relations),
                int(profile.get("relations_matched_at_least") or len(current_relations)),
            )
        except (TypeError, ValueError):
            current_relations_matched = len(current_relations)
        if not temporal_requested:
            shown_current_relations = current_relations[:_ENTITY_LOOKUP_RELATION_CAP]
            return {
                "found": True,
                "entity": public_entity,
                **safe_profile,
                "relations": [
                    _entity_lookup_relation_projection(edge, {}) for edge in shown_current_relations
                ],
                "relations_matched_at_least": current_relations_matched,
                "relations_truncated": bool(profile.get("relations_truncated"))
                or current_relations_matched > len(shown_current_relations),
            }
        # Картина на дату собирается ОТДЕЛЬНЫМ обходом, а не фильтром поверх
        # профиля: профиль уже отбросил отменённые связи, и восстановить их из
        # него нечем.
        raw_past = await run_blocking(
            kg.get_entity_graph,
            actor.user_id,
            str(entity["id"]),
            1,
            as_of=normalized_as_of,
            known_at=normalized_known_at,
        )
        past = _assert_snapshot_as_of(
            raw_past,
            as_of=normalized_as_of,
            label="entity graph snapshot",
        )
        if normalized_known_at:
            _assert_temporal_snapshot_agrees(
                past,
                history_status,
                label="entity graph snapshot",
            )
        else:
            _assert_valid_time_basis(past, label="entity graph snapshot")
        raw_nodes = past.get("nodes")
        by_id = {
            str(node.get("id")): node
            for node in (raw_nodes if isinstance(raw_nodes, list) else [])
            if isinstance(node, Mapping)
        }
        raw_edges = past.get("edges")
        edges = (
            [edge for edge in raw_edges if isinstance(edge, Mapping)] if isinstance(raw_edges, list) else []
        )
        shown_edges = edges[:_ENTITY_LOOKUP_RELATION_CAP]
        try:
            matched_edges = max(
                len(edges),
                int(past.get("edges_matched_at_least") or len(edges)),
            )
        except (TypeError, ValueError):
            matched_edges = len(edges)
        result = {
            "found": True,
            "entity": public_entity,
            **safe_profile,
            "as_of": normalized_as_of,
            **(history_status if normalized_known_at else {"known_at": "", "temporal_basis": "valid_time"}),
            "relations": [_entity_lookup_relation_projection(edge, by_id) for edge in shown_edges],
            "relations_matched_at_least": matched_edges,
            "relations_truncated": matched_edges > len(shown_edges),
        }
        if normalized_known_at:
            confirmed_status = _validated_known_at_preflight(
                await run_blocking(
                    storage.relation_history_status,
                    actor.user_id,
                    known_at=normalized_known_at,
                ),
                known_at=normalized_known_at,
            )
            if confirmed_status != history_status:
                raise ValueError("relation-history status changed during entity_lookup")
        return result

    async def _entity_create(
        self,
        *,
        actor: ActorContext,
        name: str,
        entity_type: str,
        description: str = "",
        aliases: list[str] | None = None,
    ) -> dict[str, Any]:
        _, _, _, ingestion = self._require_services()
        parsed_type = EntityType(entity_type)
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("name is required")
        clean_aliases = [str(item).strip() for item in (aliases or []) if str(item).strip()][:20]
        content_lines = [f"Предложение сущности: {clean_name}", f"Тип: {parsed_type.value}"]
        if description.strip():
            content_lines.append(f"Описание: {description.strip()}")
        if clean_aliases:
            content_lines.append("Алиасы: " + ", ".join(clean_aliases))
        return await ingestion.queue_agent_candidate(
            actor.user_id,
            "\n".join(content_lines),
            source_ref=new_id("toolref"),
            candidate_type="entity",
            metadata={
                "tool": "entity_create",
                # Тот же довод, что у `memory_save`: автор предложения — человек, а
                # не арендатор. Без этого поля повтор одного участника глушился бы
                # карточкой другого, а поиск повтора по содержимому не находил бы
                # ничего вовсе — он сверяет именно автора.
                "requested_by": actor.own_id,
                "entity_proposal": {
                    "name": clean_name,
                    "entity_type": parsed_type.value,
                    "description": description.strip()[:2000],
                    "aliases": clean_aliases,
                },
            },
            suggestion_overrides={
                "title": f"Сущность: {clean_name}",
                "knowledge_kind": "entity_proposal",
                "entities": [
                    {
                        "name": clean_name,
                        "entity_type": parsed_type.value,
                        "confidence": 0.7,
                        "evidence": description.strip() or "agent-authored entity proposal",
                    }
                ],
            },
        )

    def _open_source(self, actor: ActorContext, name: str) -> tuple[Any, str]:
        """Найти объявленный источник и строку подключения к нему.

        Строка живёт в переменной окружения, а не в базе: резервные копии архива
        переживают всё, и пароль от чужой боевой системы уехал бы в них.
        """

        import os

        from friday.data_sources import DataSource

        storage, _, _, _ = self._require_services()
        row = storage.get_data_source(actor.user_id, str(name or "").strip())
        if row is None:
            raise ValueError(f"Источник «{name}» не объявлен")
        source = DataSource(
            name=str(row["name"]),
            kind=str(row["kind"]),
            dsn_env=str(row["dsn_env"]),
            description=str(row["description"] or ""),
        )
        dsn = os.environ.get(source.dsn_env, "")
        if not dsn:
            raise ValueError(
                f"Источник «{source.name}» объявлен, но переменная {source.dsn_env} не задана — "
                "подключаться нечем"
            )
        return source, dsn

    async def _data_sources(self, *, actor: ActorContext) -> dict[str, Any]:
        """Какие внешние базы объявлены и о чём они."""

        storage, _, _, _ = self._require_services()
        rows = await run_blocking(storage.list_data_sources, actor.user_id)
        return {
            "sources": [
                {
                    "name": str(row["name"]),
                    "kind": str(row["kind"]),
                    "description": str(row["description"] or ""),
                }
                for row in rows
            ]
        }

    async def _data_schema(self, *, actor: ActorContext, source: str) -> dict[str, Any]:
        """Таблицы и столбцы источника — то, по чему составляется запрос.

        Без этого модель угадывает имена таблиц, а угаданное имя даёт не пустой
        ответ, а ОШИБКУ, и выглядит она как «источник не работает».
        """

        from friday.data_sources import describe_source

        declared, dsn = self._open_source(actor, source)
        return await run_blocking(describe_source, declared, dsn)

    async def _data_query(self, *, actor: ActorContext, source: str, sql: str) -> dict[str, Any]:
        """Одно чтение во внешней базе. Запрос возвращается вместе с ответом.

        Возвращается намеренно: человек должен видеть, ЧТО именно спросили в
        чужой системе, а не только полученные числа.
        """

        from friday.data_sources import run_query

        declared, dsn = self._open_source(actor, source)
        storage, _, _, _ = self._require_services()
        result = await run_blocking(run_query, declared, dsn, sql)
        await run_blocking(storage.touch_data_source, actor.user_id, declared.name)
        return result

    async def _relation_end(
        self,
        *,
        actor: ActorContext,
        source: str,
        target: str,
        relation_type: str = "",
        valid_to: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        """Связь КОНЧИЛАСЬ — это не то же самое, что её не было.

        До этого инструмента отмена жила только админским маршрутом: ни кнопки,
        ни команды, ни способа у модели. То есть человек мог сказать «он давно
        перевёлся», а система умела только согласиться на словах — в графе
        связь оставалась действующей и продолжала отвечать на вопросы о
        сегодняшнем дне.

        Мягкое удаление тут не годится: оно говорит «этого не было» и стирает
        прошлое. Рапорт 2024 года остаётся фактом о 2024-м после перевода.
        """

        selected_type = str(relation_type or "").strip()
        if selected_type:
            try:
                selected_type = RelationType(selected_type).value
            except ValueError:
                return {
                    "ended": False,
                    "ambiguous": False,
                    "reason": f"Неизвестный тип связи: {selected_type}",
                    "candidates": [],
                }

        _, kg, _, _ = self._require_services()
        first = await run_blocking(kg.find_entity, actor.user_id, source)
        second = await run_blocking(kg.find_entity, actor.user_id, target)
        if not first or not second:
            missing = source if not first else target
            return {
                "ended": False,
                "ambiguous": False,
                "reason": f"Объект «{missing}» в графе не найден",
                "candidates": [],
            }
        edges = await run_blocking(kg.get_entity_relations, str(first["id"]), actor.user_id)
        wanted = {str(second["id"])}
        matches = [
            edge
            for edge in edges
            if (str(edge.get("source_entity_id")) in wanted or str(edge.get("target_entity_id")) in wanted)
            and (not selected_type or str(edge.get("relation_type") or "") == selected_type)
        ]
        if not matches:
            suffix = f" типа {selected_type}" if selected_type else ""
            return {
                "ended": False,
                "ambiguous": False,
                "reason": f"Действующей связи{suffix} между ними нет",
                "candidates": [],
            }
        if len(matches) > 1:
            candidates = sorted(
                (
                    {
                        "id": str(edge["id"]),
                        "type": str(edge.get("relation_type") or ""),
                        "source": str(edge.get("source_entity_id") or ""),
                        "target": str(edge.get("target_entity_id") or ""),
                    }
                    for edge in matches
                ),
                key=lambda item: (item["type"], item["id"]),
            )
            return {
                "ended": False,
                "ambiguous": True,
                "reason": "Между объектами несколько действующих связей; укажите relation_type",
                "source": first["name"],
                "target": second["name"],
                "candidates": candidates,
            }

        edge = matches[0]
        result = await run_blocking(
            kg.invalidate_relation,
            actor.user_id,
            str(edge["id"]),
            valid_to=str(valid_to or "").strip(),
            reason=str(reason or "").strip()[:300],
        )
        ended = []
        if result:
            ended.append(
                {
                    "id": str(edge["id"]),
                    "type": edge.get("relation_type"),
                    "valid_to": result.get("valid_to") or "",
                }
            )
        return {
            "ended": bool(ended),
            "ambiguous": False,
            "relations": ended,
            "source": first["name"],
            "target": second["name"],
        }

    async def _entity_link(
        self,
        *,
        actor: ActorContext,
        source_entity_id: str,
        target_entity_id: str,
        relation_type: str,
        confidence: float = 0.65,
        evidence: str = "",
    ) -> dict[str, Any]:
        storage, _, _, _ = self._require_services()
        parsed_type = RelationType(relation_type)
        parsed_confidence = float(confidence)
        if not 0.0 <= parsed_confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        return storage.store_relation_candidate(
            actor.user_id,
            source_entity_id,
            target_entity_id,
            parsed_type.value,
            confidence=min(0.79, parsed_confidence),
            evidence={
                "source": "agent_tool",
                "note": str(evidence or "").strip()[:1000],
                "review_only": True,
            },
        )

    async def _kg_stats(self, *, actor: ActorContext) -> dict[str, Any]:
        _, kg, _, _ = self._require_services()
        return kg.get_stats(actor.user_id)

    async def _list_tags(self, *, actor: ActorContext) -> dict[str, Any]:
        """Every tag in the user's own knowledge base with its record count.

        Found by adversarial review ahead of a live multi-user demo: the
        Telegram `/tags` command already calls `storage.list_knowledge_tags`
        (via `/api/knowledge/tags`), but the agent's own tool loop had no
        equivalent — a natural-language "what tags do I have and how many
        records each" could only reach `memory_search`, which answers a
        different question (content match, not taxonomy enumeration) and
        produced an ungrounded answer that failed verification in rehearsal.
        """
        storage, _, _, _ = self._require_services()

        def read_page_and_total() -> tuple[list[dict[str, Any]], int]:
            return (
                storage.list_knowledge_tags(actor.user_id),
                storage.count_knowledge_tags(actor.user_id),
            )

        # Page membership, per-tag counts and exact distinct total are one
        # published fact.  A concurrent import/delete between independent
        # SELECTs could otherwise produce a payload that never existed.
        items, total = await run_blocking(
            _storage_read_snapshot,
            storage,
            read_page_and_total,
        )
        # Показываются САМЫЕ ЧАСТЫЕ, а общее число называется отдельно.
        #
        # Замерено на живом архиве 2026-08-02: полный список занимал 11 075 знаков
        # из 12 000 бюджета инструмента и обрезался посреди структуры — хвост
        # меток не доходил до модели вовсе, молча. Вопрос «какие у меня метки»
        # человек задаёт про заметные, а не про все восемьсот: `total` честно
        # говорит, сколько их всего, `truncated` — что показаны не все.
        shown = items[:_TAGS_SHOWN_TO_LLM]
        return {
            "tags": shown,
            "count": len(shown),
            "total": total,
            "truncated": len(shown) < total,
        }

    async def _speak(self, *, actor: ActorContext, text: str) -> dict[str, Any]:
        """Synthesize `text` as a voice clip. Call this only when the user has
        explicitly asked for a spoken reply within the conversation — most turns
        should not call it. The clip is delivered to the user by the caller
        (Telegram voice message); this tool never touches storage or other users.
        """
        del actor  # synthesis has no per-user state; kept for handler signature parity
        if not (self.settings and self.settings.tts_enabled):
            return {"spoken": False, "reason": "text-to-speech is disabled"}
        download_root = self.settings.tts_download_root or str(self.settings.model_root / "piper")
        try:
            speech = await run_blocking(
                synthesize_speech,
                text,
                voice=self.settings.tts_voice,
                download_root=download_root,
                max_chars=self.settings.tts_max_chars,
            )
        except TTSUnavailable as exc:
            LOGGER.warning("tts: unavailable (%s)", type(exc).__name__)
            return {"spoken": False, "reason": "voice engine unavailable"}
        except ValueError:
            return {"spoken": False, "reason": "nothing to speak"}
        return {
            "spoken": True,
            # Длина ОЗВУЧЕННОГО, а не исходного: раньше здесь стояла длина текста,
            # который передали на синтез, и даже модель не знала, сколько из него
            # прозвучало.
            "chars": min(len(text), int(self.settings.tts_max_chars)),
            "duration_sec": speech.duration_sec,
            "truncated": speech.truncated,
            "_attachment": {
                "kind": "voice",
                "mime_type": "audio/ogg",
                "audio_base64": base64.b64encode(speech.audio_bytes).decode("ascii"),
                "duration_sec": speech.duration_sec,
                # Мост дописывает человеку строку об обрыве: услышать половину
                # ответа и не узнать об этом хуже, чем прочитать оговорку.
                "truncated": speech.truncated,
            },
        }

    async def _make_file(
        self,
        *,
        actor: ActorContext,
        kind: str,
        title: str,
        blocks: list[dict[str, Any]] | None = None,
        subtitle: str = "",
        filename: str = "",
    ) -> dict[str, Any]:
        """Собрать готовый файл: Word, Excel, PDF или картинку.

        Требование владельца (2026-08-01): «сделай мне отчёт с выводом по тем-то
        документам» должно заканчиваться файлом, а не текстом в чате.

        Содержимое описывается структурой (заголовки, абзацы, списки, таблицы), а
        не разметкой формата: иначе модель учила бы три разных языка разметки, и
        форматы разошлись бы по возможностям в первый же день.

        Инструмент НИЧЕГО не выдумывает и ничего не ищет: что писать, решает
        модель по уже собранным основаниям. Здесь — только вёрстка.
        """
        del actor
        spec = spec_from_payload(title, subtitle, blocks or [])
        if not spec.blocks:
            return {"created": False, "reason": "нечего писать: не передано ни одного блока"}
        try:
            payload = await run_blocking(render, kind, spec)
        except ValueError:
            return {"created": False, "reason": "не удалось собрать файл: ValueError"}
        except Exception as exc:  # noqa: BLE001 — вёрстка не должна ронять ход
            LOGGER.warning("Report rendering failed (%s)", type(exc).__name__)
            return {"created": False, "reason": f"не удалось собрать файл: {type(exc).__name__}"}
        if len(payload) > _MAX_GENERATED_FILE_BYTES:
            return {
                "created": False,
                "reason": f"файл получился {len(payload) // 1024} КБ — больше допустимого",
            }
        mime, extension = SUPPORTED_KINDS[str(kind).strip().casefold()]
        name = _safe_filename(filename or spec.title, extension)
        return {
            "created": True,
            "filename": name,
            "kind": kind,
            "bytes": len(payload),
            "_attachment": {
                "kind": "document",
                "filename": name,
                "mime_type": mime,
                "content_base64": base64.b64encode(payload).decode("ascii"),
            },
        }

    def _days_meant(self, days: list[str]) -> tuple[list[str], list[str]]:
        """Что человек назвал числами — в полные даты. Возврат: (даты, непонятое).

        Владелец просит «за 10, 13 и 25 число», а не «с 2026-08-10 по
        2026-08-25»: между этими числами лежат две недели чужих файлов. Поэтому
        дни идут списком и достраиваются поштучно.

        Голое число — ПОСЛЕДНЕЕ такое число, уже наступившее. «Собери за 25-е»,
        сказанное 3 августа, означает 25 июля: 25 августа ещё не было, и пустой
        архив был бы формально правильным и бесполезным ответом.

        Непонятое возвращается отдельно, а не отбрасывается: человек назвал
        что-то, чего он в архиве не увидит, и должен об этом узнать.
        """
        today = datetime.now(self._zone()).date()
        out: list[str] = []
        unclear: list[str] = []
        for raw in days:
            token = str(raw or "").strip()
            if not token:
                continue
            try:
                out.append(date.fromisoformat(token).isoformat())
                continue
            except ValueError:
                pass
            digits = token.strip(" -.,;«»\"'()числаго")
            if digits.isdigit() and 1 <= int(digits) <= 31:
                number = int(digits)
                year, month = today.year, today.month
                if number > today.day:
                    # Этого числа в текущем месяце ещё не было — значит речь о
                    # прошлом месяце. Декабрь предыдущего года считается тем же
                    # правилом, а не отдельной веткой.
                    month -= 1
                    if month == 0:
                        month, year = 12, year - 1
                try:
                    out.append(date(year, month, number).isoformat())
                except ValueError:
                    # 31-е в тридцатидневном месяце. Такого дня не было, и
                    # придумывать ему замену — врать о том, что человек просил.
                    unclear.append(token)
                continue
            unclear.append(token)
        # Порядок сохранён, повторы убраны: «10, 13 и снова 10» не должно
        # положить один и тот же день дважды.
        seen: set[str] = set()
        unique: list[str] = []
        for day in out:
            if day not in seen:
                seen.add(day)
                unique.append(day)
        return unique, unclear

    async def _collect_files(
        self,
        *,
        actor: ActorContext,
        days: list[str] | None = None,
        name: str = "",
    ) -> dict[str, Any]:
        """Собрать пришедшие файлы в один архив и отдать человеку.

        Владелец 2026-08-03: «Пятница же не умеет архивы собирать? Надо, чтобы
        умела: собрать документы, пришедшие за 10, 13 и 25 число».

        Кладутся ИСХОДНЫЕ файлы, а не пересказ: просили документы. `make_file`
        рядом решает другую задачу — сочинить новый документ по основаниям.

        Что не поместилось, перечисляется поимённо. Молчаливый обрез за сутки
        2026-08-01 нашёлся четырежды в разных подсистемах, и здесь он опаснее
        обычного: человек унесёт архив с собой, считая его полным.
        """
        storage, _, _, _ = self._require_services()
        settings = self.settings
        if settings is None:
            return {"collected": False, "reason": "хранилище файлов не настроено"}
        wanted, unclear = self._days_meant(list(days or []))
        if not wanted:
            return {
                "collected": False,
                "reason": "не понял, за какие дни собирать",
                "unclear_days": unclear,
            }
        offset = int(datetime.now(self._zone()).utcoffset().total_seconds() // 60)  # type: ignore[union-attr]
        rows = await run_blocking(
            storage.list_files_received_on,
            actor.user_id,
            days=wanted,
            utc_offset_minutes=offset,
            limit=_MAX_ARCHIVE_FILES + 1,
        )
        if not rows:
            return {
                "collected": False,
                "reason": "за эти дни файлов не приходило",
                "days": wanted,
                "unclear_days": unclear,
                "found": 0,
            }
        # Выборка берётся на один больше потолка — чтобы отличить «ровно потолок»
        # от «больше потолка», — а пакуется ровно потолок. Без среза в архив
        # уезжал 301-й файл, и «вошли первые 300» было бы неправдой.
        page = rows[:_MAX_ARCHIVE_FILES]
        packed, skipped, size, total, packed_count = await run_blocking(
            _pack_authorized_archive,
            storage,
            Path(settings.files_dir),
            page,
            name or "archive",
            user_id=actor.user_id,
            days=wanted,
            utc_offset_minutes=offset,
        )
        if not packed:
            return {
                "collected": False,
                "reason": "не удалось собрать архив",
                "days": wanted,
                "found": total,
            }
        filename = _safe_filename(name or f"Документы за {', '.join(wanted)}", "zip")
        result: dict[str, Any] = {
            "collected": True,
            "days": wanted,
            "files_in_archive": packed_count,
            "found_total": total,
            "bytes": size,
            "filename": filename,
            "_attachment": {
                "kind": "document",
                "filename": filename,
                "mime_type": "application/zip",
                "content_base64": base64.b64encode(packed).decode("ascii"),
            },
        }
        if unclear:
            result["unclear_days"] = unclear
        if skipped:
            # Поимённо, а не числом: «пропущено 12» человек прочитает как мелочь,
            # а среди этих двенадцати может лежать именно тот документ, за
            # которым он и пришёл.
            result["left_out"] = skipped[:20]
            result["left_out_count"] = len(skipped)
        if total > packed_count:
            # Готовая фраза, а не слагаемые. Замерено на живом экземпляре
            # 2026-08-03: инструмент отдавал «файлов 1671, вошли первые 160» и
            # отдельно 140 пропущенных имён — модель сложила это по-своему и
            # сказала человеку «остальные 140 не поместились», хотя не вошло
            # 1511. Складывать числа она не обязана, и там, где ошибка меняет
            # смысл, считать должен код.
            missed = total - packed_count
            detail = f"за эти дни файлов {total}, в архив вошло {packed_count}, не вошло {missed}"
            if skipped:
                detail += (
                    f" (из них {len(skipped)} не поместились по объёму или потерялись, "
                    f"остальные не рассматривались: за один раз берётся не больше "
                    f"{_MAX_ARCHIVE_FILES} файлов)"
                )
            result["not_all"] = detail
        return result

    async def _resolve_duplicates(self, *, actor: ActorContext) -> dict[str, Any]:
        _, kg, _, _ = self._require_services()
        # Off the event loop: the scan is quadratic in entity count and this is a
        # tool the agent calls mid-conversation.
        #
        # A budgeted tick, and the candidates come from the STORED table rather than
        # from whatever this tick happened to reach. Otherwise an agent that ran the
        # scan mid-sweep would report the slice it saw as the whole answer — and it
        # answers in prose, where a caveat is easy to drop.
        report = await run_blocking(kg.resolver.sweep_duplicates, actor.user_id)
        pending = await run_blocking(kg.resolver.get_pending_resolutions, actor.user_id)
        return {
            "candidates": pending,
            "count": len(pending),
            "scan": report,
            "complete": bool(report.get("complete")),
        }

    async def _conflict_list(self, *, actor: ActorContext, limit: int = 5) -> dict[str, Any]:
        """Next pending knowledge conflicts for the actor — a page, not the whole queue.

        Two hundred suggested conflicts on the live install, and zero paths from
        chat until this tool and the matching /conflicts command. Portions only:
        nobody reviews two hundred in one reply.

        Each item carries a ``triage`` hint (likely_duplicate /
        likely_different_records / uncertain) computed from the same features as
        the queue probe — a label for the reviewer, not an automatic decision.
        """
        from friday.conflict_triage import attach_conflict_hint
        from friday.knowledge_graph import _safe_conflict_card
        from friday.storage._knowledge import _bounded_knowledge_conflict_rows

        storage, _, _, _ = self._require_services()
        page = max(1, min(int(limit), 10))
        items = await run_blocking(
            _bounded_knowledge_conflict_rows,
            storage,
            actor.user_id,
            status="suggested",
            limit=page,
            offset=0,
        )
        total = await run_blocking(storage.count_knowledge_conflicts, actor.user_id, status="suggested")

        def _compact_page() -> list[dict[str, Any]]:
            compact: list[dict[str, Any]] = []
            for item in items:
                enriched = attach_conflict_hint(storage, actor.user_id, item)
                safe = _safe_conflict_card(enriched)
                compact.append(
                    {
                        "id": safe["id"],
                        "conflict_type": safe["conflict_type"],
                        "confidence": safe["confidence"],
                        "triage": safe.get("triage") or {},
                        "a": {
                            "id": safe["knowledge_a_id"],
                            "title": safe["knowledge_a_title"],
                            "summary": safe["knowledge_a_summary"],
                        },
                        "b": {
                            "id": safe["knowledge_b_id"],
                            "title": safe["knowledge_b_title"],
                            "summary": safe["knowledge_b_summary"],
                        },
                        "evidence": safe["evidence"],
                    }
                )
            return compact

        compact = await run_blocking(_compact_page)
        return {
            "count": len(compact),
            "total": total,
            "truncated": len(compact) < total,
            "items": compact,
        }

    async def _conflict_decide(
        self,
        *,
        actor: ActorContext,
        conflict_id: str,
        decision: str,
    ) -> dict[str, Any]:
        """Settle one suggested conflict. Does not re-open a terminal decision.

        decision:
          - dismiss — not a real conflict; both records stay as they are
          - keep_a / keep_b — resolve by keeping that side; the other is deprecated
        """
        _, kg, _, _ = self._require_services()
        from friday.knowledge_graph import _safe_conflict_result
        from friday.storage._knowledge import _bounded_knowledge_conflict_by_id

        choice = str(decision or "").casefold().strip()
        if choice not in {"dismiss", "keep_a", "keep_b"}:
            raise ValueError("decision must be dismiss, keep_a or keep_b")
        conflict = await run_blocking(
            _bounded_knowledge_conflict_by_id,
            kg.storage,
            actor.user_id,
            conflict_id,
        )
        if not conflict:
            raise ValueError("Conflict not found")
        if str(conflict.get("status") or "") != "suggested":
            raise ValueError(f"Conflict is already {conflict.get('status')}")
        if choice == "dismiss":
            result = await run_blocking(
                kg.review_conflict,
                actor.user_id,
                conflict_id,
                "dismissed",
                reviewed_by=actor.own_id,
                resolution_note="telegram/agent: dismissed",
            )
            if not result:
                raise ValueError("Conflict not found")
            return {
                "status": "dismissed",
                "conflict_id": conflict_id,
                "item": _safe_conflict_result(result),
            }
        winner_id = str(conflict["knowledge_a_id"]) if choice == "keep_a" else str(conflict["knowledge_b_id"])
        result = await run_blocking(
            kg.resolve_conflict,
            actor.user_id,
            conflict_id,
            winner_id,
            reviewed_by=actor.own_id,
            resolution_note=f"telegram/agent: {choice}",
        )
        if not result:
            raise ValueError("Conflict not found")
        return {
            "status": "resolved",
            "conflict_id": conflict_id,
            "winner_id": winner_id,
            "item": _safe_conflict_result(result),
        }

    async def _entity_merge_decide(
        self,
        *,
        actor: ActorContext,
        candidate_id: str,
        decision: str,
        target_entity_id: str | None = None,
    ) -> dict[str, Any]:
        """Accept or reject one entity-merge candidate. Complements resolve_duplicates."""
        _, kg, _, _ = self._require_services()
        choice = str(decision or "").casefold().strip()
        if choice not in {"accept", "reject"}:
            raise ValueError("decision must be accept or reject")
        if choice == "reject":
            await run_blocking(
                kg.resolver.reject_resolution,
                candidate_id,
                actor.user_id,
                resolved_by=actor.own_id,
            )
            return {"status": "rejected", "candidate_id": candidate_id}
        merged = await run_blocking(
            kg.resolver.accept_resolution,
            candidate_id,
            actor.user_id,
            target_entity_id=target_entity_id,
            resolved_by=actor.own_id,
        )
        from friday.knowledge_graph import _safe_merge_result

        return {
            "status": "merged",
            "candidate_id": candidate_id,
            "result": _safe_merge_result(merged),
        }

    async def _entity_merge_undo(
        self,
        *,
        actor: ActorContext,
        merge_id: str,
    ) -> dict[str, Any]:
        """Undo one accepted entity merge. Needs the transfer set written at merge time."""
        _, kg, _, _ = self._require_services()
        result = await run_blocking(
            kg.resolver.unmerge,
            actor.user_id,
            merge_id,
            undone_by=actor.own_id,
        )
        from friday.knowledge_graph import _safe_merge_result

        return {
            "status": "undone",
            "merge_id": merge_id,
            "result": _safe_merge_result(result),
        }

    async def _inbox_list(self, *, actor: ActorContext, status: str | None = None) -> dict[str, Any]:
        storage, _, _, _ = self._require_services()
        status_value = InboxStatus(status) if status else None
        # Двенадцать, а не двадцать: разбирают входящие по одному, и вопрос «что
        # там накопилось» требует обзора, а не всей очереди. Сколько её на самом
        # деле, говорит `total`.
        rows = storage.list_inbox(actor.user_id, status_value, limit=12)
        # `count` — сколько ПОКАЗАНО, `total` — сколько есть. Возвращать длину среза
        # под именем count значит сказать модели «у вас 20 входящих» при двухстах, а
        # модель перескажет это человеку прозой, где оговорку уже не восстановить.
        # Соседний инструмент в этом же файле решает ровно эту задачу явно.
        total = storage.count_inbox(actor.user_id, status_value)
        return {
            "items": [_inbox_row_for_llm(row) for row in rows],
            "count": len(rows),
            "total": total,
            "truncated": len(rows) < total,
        }

    async def _user_activity(
        self,
        *,
        actor: ActorContext,
        person: str,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        analysis: list[str] | None = None,
        top: int = 10,
    ) -> dict[str, Any]:
        """What one account wrote and uploaded — reachable by the name a person uses.

        The only tool that reads across accounts, so three things are non-negotiable.
        The gate is a capability checked by `execute` like every other tool. The read
        is written to the audit log against the account it was about, not just the
        tool name. And the account it resolved to is returned in the answer:
        `resolve_person` is tolerant of case endings, layout and typos, and a tolerant
        match that is wrong must be visible to whoever reads the reply rather than
        buried under a confident-sounding summary.

        The gate is the LOWER of the two levels, `admin.activity.read`, and the body
        is disclosed only to an actor who also holds `admin.all_data.read`. Gating on
        the higher one instead would have shut the metadata tier out of the tool
        entirely; gating on the lower one without this second check would have handed
        it every body through the agent, which is the one surface where the
        distinction is easiest to lose.
        """
        storage, _, _, _ = self._require_services()
        include_content = bool(
            self.authorization and self.authorization.authorize(actor, "admin.all_data.read").allowed
        )
        matches = resolve_person(storage.list_users(limit=5000), person)
        chosen = unambiguous(matches)
        if chosen is None:
            # Nobody, or more than one. Either way the caller decides, not this tool.
            #
            # Recorded even so. The reply carries up to five accounts — id, display
            # name, username — and returning early meant the account list of the
            # machine could be enumerated by feeding ambiguous names, leaving no
            # trace at all. The tool's own description promises that reading another
            # account is written down; that promise has to hold on this branch too.
            storage.log_audit(
                AuditEntry(
                    id=new_id("audit"),
                    user_id=actor.own_id,
                    action="tool.user_activity.unresolved",
                    target_type="user",
                    target_id="*",
                    after_json={
                        "asked_for": person[:200],
                        "reason": "ambiguous" if matches else "not_found",
                        "candidates": len(matches),
                    },
                )
            )
            return {
                "resolved": None,
                "candidates": [match.to_dict() for match in matches[:5]],
                "reason": "ambiguous" if matches else "not_found",
            }

        # Право надзора говорит «можно смотреть чужое», но не «можно смотреть
        # ЛЮБОГО». Владелец просил заводить каждого написавшего с полными
        # правами — значит право надзора есть у всех, и без этой проверки любой
        # участник читал бы деятельность любого другого.
        if hierarchy_is_configured(storage) and not may_oversee(
            storage, actor.own_id, chosen.user_id, owner_id=LEGACY_OWNER_USER_ID
        ):
            storage.log_audit(
                AuditEntry(
                    id=new_id("audit"),
                    user_id=actor.own_id,
                    action="tool.user_activity.out_of_scope",
                    target_type="user",
                    target_id=chosen.user_id,
                    after_json={"asked_for": person[:200]},
                )
            )
            return {
                "resolved": chosen.to_dict(),
                "denied": True,
                "reason": (
                    "Это не ваш подчинённый. Смотреть деятельность можно у себя и у тех, "
                    "кто вам подчинён; полный доступ есть у владельца архива."
                ),
            }

        storage.log_audit(
            AuditEntry(
                id=new_id("audit"),
                user_id=actor.own_id,
                action="tool.user_activity",
                target_type="user",
                target_id=chosen.user_id,
                after_json={
                    "asked_for": person[:200],
                    "match_method": chosen.method,
                    "since": since,
                    "until": until,
                    "content": "full" if include_content else "redacted",
                    "analysis": list(analysis) if analysis else None,
                },
            )
        )
        # ГДЕ лежит материал и ЧЕЙ он — разные вопросы, и здесь они разведены.
        #
        # В общем архиве `raw_objects.user_id` — арендатор, один на всех, а
        # человека называет только пометка `uploaded_by`. Прежний вызов передавал
        # сюда человека как арендатора: строк с таким `user_id` в базе нет вовсе,
        # то есть надзор по загрузкам отвечал пустотой ВСЕГДА — и пустота
        # читалась как «он ничего не присылал».
        #
        # Различает ОБЩИЙ ли архив, а не «совпали ли идентификаторы». Первая
        # редакция сравнивала арендатора с человеком — и в обычной установке, где
        # у каждого своя учётка, включала фильтр по автору там, где материал
        # лежит под собственным `user_id` и пометки может не иметь вовсе.
        # Поймано собственным набором: восемь упавших тестов.
        shared = bool(getattr(actor, "shared_tenant", False))
        tenant = actor.user_id if shared else chosen.user_id
        by_author = chosen.user_id if shared else ""
        answer: dict[str, Any] = {
            "resolved": chosen.to_dict(),
            "content": "full" if include_content else "redacted",
            "summary": storage.user_activity_summary(tenant, since=since, until=until, uploaded_by=by_author),
            # Что человек ПИСАЛ. Без этого инструмент выполнял своё название
            # наполовину: у того, кто только переписывается, загрузок ноль, и на
            # «что писал JBL?» приходило «сообщений 42, но записи не загрузились».
            "messages": storage.user_messages(
                chosen.user_id,
                since=since,
                until=until,
                limit=max(1, min(int(limit), 40)),
                include_content=include_content,
            ),
            "items": storage.user_activity(
                tenant,
                since=since,
                until=until,
                limit=max(1, min(int(limit), 200)),
                include_content=include_content,
                uploaded_by=by_author,
            ),
            # Сколько документов за то же окно вообще НЕ имеют отметки автора.
            #
            # Без этого числа «загрузок от него нет» звучит одинаково в двух совсем
            # разных случаях: когда все загрузки подписаны и среди них его нет, и
            # когда не подписана ни одна. На живой базе сегодня второе — 3292
            # документа из 3292 без автора, потому что признак появился позже них.
            #
            # Число, а не флаг: оно само по себе стареет в нужную сторону. По мере
            # того как новые документы приходят уже с отметкой, безымянных в
            # СВЕЖЕМ окне становится ноль — и отрицание делается законным, без
            # единой правки здесь.
            "arrivals_without_an_author": storage.arrivals_without_an_author(tenant, since, until),
        }
        if analysis:
            try:
                answer["analysis"] = storage.user_activity_analysis(
                    tenant,
                    since=since,
                    until=until,
                    analyses=list(analysis),
                    top=max(1, min(int(top), 50)),
                    include_content=include_content,
                    uploaded_by=by_author,
                )
            except ValueError:
                # The schema constrains the enum, so this is reachable only if the
                # vocabulary and the schema drift apart. Say which values exist
                # rather than failing the whole call.
                answer["analysis_error"] = "недопустимый вид анализа"
        return _person_answer_for_llm(answer, zone=self._zone())

    async def _user_knowledge_search(
        self,
        *,
        actor: ActorContext,
        person: str,
        query: str,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Ответить ПО СУЩЕСТВУ по корпусу названного человека, не сливая корпуса.

        `user_activity` отвечает про объём и темы — «что прислал, когда, сколько».
        На вопрос «что он писал про сроки поставки» она ответить не может, а
        обычный поиск ограничен своим арендатором by design. Между ними была
        дыра: старший, которому владелец разрешил видеть содержимое, читал чужое
        только глазами, листая ленту активности.

        Изоляция при этом НЕ снимается: арендатор остаётся параметром поиска, а
        не исчезает из него. Меняется ровно одно — кто вправе назвать чужой
        арендатор этим параметром, и это право проверяется здесь.

        Гейт — ВЕРХНЕЕ право `admin.all_data.read`, в отличие от `user_activity`,
        где нижнего хватает на метаданные: здесь метаданных не бывает, любой
        результат — содержимое. Отказ обязан быть честным («объём вижу,
        написанное — нет»), иначе он неотличим от «ничего не нашлось».

        Аудит пишется на ЦЕЛЕВОЙ аккаунт и несёт сам вопрос: «кто-то читал
        Иванова» без вопроса не даёт разобраться, что именно искали.
        """
        storage, _, _, _ = self._require_services()
        if not (self.authorization and self.authorization.authorize(actor, "admin.all_data.read").allowed):
            return {
                "resolved": None,
                "reason": "content_not_permitted",
                "hint": (
                    "Доступен объём и темы через user_activity; сам текст чужих записей "
                    "требует прав полного администратора."
                ),
            }
        clean_query = " ".join(str(query or "").split()).strip()
        if not clean_query:
            return {"resolved": None, "reason": "empty_query"}
        matches = resolve_person(storage.list_users(limit=5000), person)
        chosen = unambiguous(matches)
        if chosen is None:
            # Та же ветка, что у `user_activity`, и по той же причине: ответ несёт
            # до пяти аккаунтов, поэтому перебор неоднозначных имён — это способ
            # перечислить аккаунты машины, и он тоже обязан оставлять след.
            storage.log_audit(
                AuditEntry(
                    id=new_id("audit"),
                    user_id=actor.own_id,
                    action="tool.user_knowledge_search.unresolved",
                    target_type="user",
                    target_id="*",
                    after_json={
                        "asked_for": person[:200],
                        "reason": "ambiguous" if matches else "not_found",
                        "candidates": len(matches),
                    },
                )
            )
            return {
                "resolved": None,
                "candidates": [match.to_dict() for match in matches[:5]],
                "reason": "ambiguous" if matches else "not_found",
            }

        # Тот же предел, что у `user_activity`: право надзора не означает право
        # на ЛЮБОГО. Иначе поиск по чужому архиву обходил бы проверку, которую
        # соседний инструмент уже делает.
        if hierarchy_is_configured(storage) and not may_oversee(
            storage, actor.own_id, chosen.user_id, owner_id=LEGACY_OWNER_USER_ID
        ):
            storage.log_audit(
                AuditEntry(
                    id=new_id("audit"),
                    user_id=actor.own_id,
                    action="tool.user_knowledge_search.out_of_scope",
                    target_type="user",
                    target_id=chosen.user_id,
                    after_json={"asked_for": person[:200]},
                )
            )
            return {
                "resolved": chosen.to_dict(),
                "denied": True,
                "reason": (
                    "Это не ваш подчинённый. Искать по чужим материалам можно у тех, кто вам "
                    "подчинён; полный доступ есть у владельца архива."
                ),
            }

        # Clamp on both branches: execute() does not enforce JSON-schema max, so
        # the bare storage fallback used to honour an unbounded limit while
        # HybridSearcher stayed capped at 20. Same bound as the declared schema.
        clamped_limit = max(1, min(int(limit), 20))
        scoped_hybrid = False
        if actor.shared_tenant and self.searcher is not None:
            # The searcher carries the exact Raw uploader through every candidate
            # lane before its cap.  It deliberately disables graph/entity expansion:
            # shared graph rows do not yet have trustworthy author provenance.
            found = await self.searcher.search(
                actor.user_id,
                clean_query,
                limit=clamped_limit,
                uploaded_by=chosen.user_id,
                record_usage=False,
                include_entities=False,
                graph_expansion=False,
            )
            scoped_hybrid = True
        elif actor.shared_tenant:
            # WHERE the material lives and WHO supplied it are different axes in
            # the shared archive.  A tenant-wide HybridSearcher would let foreign
            # FTS/recent/dense/graph candidates fill every cap before a Python
            # post-filter, and the same mistake without that filter would expose
            # another person's documents.  The dedicated storage lane joins the
            # source Raw provenance and applies exact `uploaded_by` before the
            # FTS/LIKE LIMIT. Unknown authors belong to nobody. This remains the
            # no-searcher fallback for offline/minimal deployments.
            found = {
                "results": await run_blocking(
                    storage.search_knowledge,
                    actor.user_id,
                    clean_query,
                    limit=clamped_limit,
                    uploaded_by=chosen.user_id,
                )
            }
        elif self.searcher is not None:
            found = await self.searcher.search(chosen.user_id, clean_query, limit=clamped_limit)
        else:
            found = {
                "results": await run_blocking(
                    storage.search_knowledge,
                    chosen.user_id,
                    clean_query,
                    limit=clamped_limit,
                )
            }
        rows = list(found.get("results") or [])
        strategy = found.get("strategy")
        dropped = 0
        if isinstance(strategy, dict):
            try:
                dropped = int(strategy.get("rerank_dropped") or 0)
            except (TypeError, ValueError):
                dropped = 0
        storage.log_audit(
            AuditEntry(
                id=new_id("audit"),
                user_id=actor.own_id,
                action="tool.user_knowledge_search",
                target_type="user",
                target_id=chosen.user_id,
                after_json={
                    "asked_for": person[:200],
                    "match_method": chosen.method,
                    # Audit storage fingerprints this locally: only SHA256 and
                    # character count persist, never the raw private question.
                    "query": clean_query[:500],
                    "shown": len(rows),
                    "filtered_out": dropped,
                },
            )
        )
        answer: dict[str, Any] = {
            "resolved": chosen.to_dict(),
            "count": len(rows),
            "query": clean_query,
        }
        if actor.shared_tenant:
            # Legacy rows without an author are excluded rather than guessed, so an
            # empty result is not a claim that no unattributed historical material
            # exists. Graph remains excluded on both paths until its own provenance
            # can carry an exact uploader boundary.
            answer["strategy"] = "scoped_hybrid" if scoped_hybrid else "scoped_lexical"
            answer["unattributed_excluded"] = True
        if dropped:
            answer["filtered_out"] = dropped

        query_for_excerpt = str(found.get("query") or clean_query)

        def scoped_excerpt(row: Mapping[str, Any]) -> str:
            """Quote the passage that carried dense recall, not the file header."""

            content = str(row.get("content") or "")
            region = content
            span = row.get("_embedding_chunk_span")
            if isinstance(span, (list, tuple)) and len(span) == 2:
                try:
                    start, end = int(span[0]), int(span[1])
                except (TypeError, ValueError):
                    start = end = -1
                if 0 <= start < end <= len(content):
                    region = content[start:end]
            return best_snippet(query_for_excerpt, region, max_chars=_TOOL_EXCERPT_CHARS)

        answer["results"] = [
            {
                "id": str(row.get("id") or ""),
                "title": str(row.get("title") or "Без названия")[:200],
                "kind": str(row.get("knowledge_kind") or "note"),
                "updated_at": row.get("updated_at"),
                "excerpt": scoped_excerpt(row),
            }
            for row in rows
        ]
        return answer

    async def _code_run(self, *, actor: ActorContext, code: str) -> dict[str, Any]:
        del actor
        settings = self.settings
        if settings is None or not settings.code_execution_enabled:
            raise ValueError("Code execution is disabled")
        if len(code) > 100_000:
            raise ValueError("Code is too large")
        # This is defense-in-depth, not an OS security boundary. Production
        # deployments should replace it with an isolated container executor.
        with tempfile.TemporaryDirectory(prefix="jericho-code-") as directory:
            script = Path(directory) / "main.py"
            with open_private_text_write(script) as handle:
                handle.write(code)
            env = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            preexec_fn = None
            if os.name == "posix":
                import resource

                # Every rlimit on Linux is PER PROCESS and is inherited by children as
                # their OWN budget, so the 512 MiB ceiling below multiplied by the number
                # of forks: measured, four forks held 1037 MiB while each reported an
                # address-space limit of exactly 512 MiB. RLIMIT_NPROC is what makes the
                # per-process limits add up to a total, because it is checked at fork
                # time against the real UID's whole task count — so the ceiling has to
                # be relative to what that count already is, or the executor could not
                # start at all. Twenty-four is room for a helper process and its
                # threads, not for a bomb.
                nproc_ceiling = _count_user_tasks() + 24

                def _limit_resources() -> None:
                    with suppress(ValueError, OSError):
                        resource.setrlimit(resource.RLIMIT_NPROC, (nproc_ceiling, nproc_ceiling))
                    resource.setrlimit(
                        resource.RLIMIT_CPU,
                        (
                            settings.code_execution_timeout_sec,
                            settings.code_execution_timeout_sec + 1,
                        ),
                    )
                    resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024, 4 * 1024 * 1024))
                    memory = 512 * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))

                preexec_fn = _limit_resources
            process_options: dict[str, Any] = {"preexec_fn": preexec_fn}
            if os.name == "posix":
                process_options["start_new_session"] = True
            elif os.name == "nt":
                process_options["creationflags"] = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                "-B",
                str(script),
                cwd=directory,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **process_options,
            )
            max_bytes = settings.code_execution_max_output_bytes
            stdout, stderr, truncated, terminated_for_limit = await _collect_bounded_process_output(
                process, max_bytes
            )
            return {
                "exit_code": process.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "output_truncated": truncated,
                "terminated_for_output_limit": terminated_for_limit,
                "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
                "security_boundary": "restricted subprocess; not an OS sandbox",
            }

    def _register_specs(self) -> None:
        def spec(
            name: str,
            description: str,
            security_id: str,
            properties: dict[str, Any],
            required: list[str],
            risk: str,
        ) -> None:
            if risk not in {"observe", "mutate", "high"}:
                raise ValueError(f"unknown risk class {risk!r} for tool {name!r}")
            self.register(
                ToolSpec(
                    name=name,
                    description=description,
                    security_id=security_id,
                    risk=risk,
                    allowed_execution_scopes=(
                        frozenset({"dialogue", "mission"})
                        if name in MISSION_EXECUTION_TOOLS
                        else frozenset({"dialogue"})
                    ),
                    parameters={
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                )
            )

        spec(
            "memory_search",
            # Про `filtered_out` сказано ЗДЕСЬ, потому что это единственный текст об
            # инструменте, который видит модель. Без пояснения `count: 0` рядом с
            # `filtered_out: 20` она прочтёт как «в архиве пусто» — а это ровно
            # противоположное: похожее есть, но отвечающего среди него нет.
            "Поиск по личной базе знаний. Если в ответе есть filtered_out, столько "
            "похожих записей нашлось и было отброшено как не отвечающие на вопрос: "
            "материал в архиве есть, но ответа в нём нет — так и скажи, не выдавая "
            "это за пустой архив. since/until ограничивают выдачу периодом по дате документа: "
            "ГГГГ-ММ-ДД, либо ГГГГ-ММ или ГГГГ — они означают весь месяц и весь год. "
            "matched_at_least — сколько документов подошло, shown — сколько из них "
            "показано в этом ответе. "
            "Непонятную запись периода инструмент отвергает, а не ищет по всему архиву. "
            "берётся собственная дата документа, а при её отсутствии — даты, упомянутые "
            "в тексте. Если в ответе empty_because=date_window, то в архиве материал "
            "есть, но не в этом периоде — скажи именно так. as_of задаёт отдельный "
            "valid-time снимок графовых связей на календарную дату; используй его для "
            "вопросов «кто с кем был связан тогда». Неверный as_of отвергается, а не "
            "подменяется сегодняшней картиной. known_at задаёт transaction-time: что "
            "Пятница уже знала к точному моменту RFC3339 с часовым поясом. Это не дата "
            "действия факта и не замена as_of; неверный или неполный снимок отклоняется.",
            "search.use",
            {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "since": {"type": "string", "description": "ГГГГ-ММ-ДД, начало периода"},
                "until": {"type": "string", "description": "ГГГГ-ММ-ДД, конец периода"},
                "as_of": {
                    "type": "string",
                    "description": "Дата ГГГГ-ММ-ДД: графовые связи, верные на этот день",
                },
                "known_at": {
                    "type": "string",
                    "format": "date-time",
                    "description": (
                        "Transaction-time RFC3339 с UTC offset: что уже было известно "
                        "Пятнице к этому точному моменту"
                    ),
                },
            },
            ["query"],
            risk="observe",
        )
        spec(
            "message_search",
            "Поиск по ИСТОРИИ ПЕРЕПИСКИ этого пользователя, а не по базе знаний. "
            "Используй, когда человек спрашивает, что он уже писал или спрашивал, "
            "а не что сохранено как заметка. memory_search — про знания; этот — "
            "про сообщения. conversation_id сужает до одного разговора.",
            "search.use",
            {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "conversation_id": {
                    "type": "string",
                    "description": "опционально: искать только в этом разговоре",
                },
            },
            ["query"],
            risk="observe",
        )
        spec(
            "memory_save",
            "Предложить заметку для Inbox review; не сохраняет знание автоматически.",
            "knowledge.create",
            {
                "content": {"type": "string"},
                "title": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
            },
            ["content"],
            risk="mutate",
        )
        spec(
            "web_search",
            "Поиск в открытом интернете. site — один строгий домен; include_domains и "
            "exclude_domains — строгие списки доменов (site нельзя сочетать с include_domains). "
            "freshness ограничивает окно значениями day/week/month/year. lang и region задают "
            "языковую и рыночную локализацию выдачи, но не гарантируют язык каждого документа "
            "или географию владельца сайта.",
            "web.search",
            {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                "site": {
                    "type": "string",
                    "maxLength": 254,
                    "pattern": r"^[^/:@?#\\\s]+\.?$",
                    "description": "hostname/domain only; no URL, path, userinfo or port",
                },
                "freshness": {"type": "string", "enum": list(SEARCH_FRESHNESS_VALUES)},
                "include_domains": {
                    "type": "array",
                    "maxItems": SEARCH_DOMAIN_LIST_MAX,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "maxLength": 254,
                        "pattern": r"^[^/:@?#\\\s]+\.?$",
                    },
                    "description": "bare hostnames; strict exact-host/subdomain allow-list",
                },
                "exclude_domains": {
                    "type": "array",
                    "maxItems": SEARCH_DOMAIN_LIST_MAX,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "maxLength": 254,
                        "pattern": r"^[^/:@?#\\\s]+\.?$",
                    },
                    "description": "bare hostnames; strict local deny-list",
                },
                "lang": {
                    "type": "string",
                    "pattern": r"^(?:[A-Za-z]{2})?$",
                    "description": "ISO-639-1 language localisation, for example ru or en",
                },
                "region": {
                    "type": "string",
                    "pattern": r"^(?:[A-Za-z]{2})?$",
                    "description": "ISO-3166-1 alpha-2 market, for example RU, US or GB",
                },
            },
            ["query"],
            risk="observe",
        )
        spec(
            "web_fetch",
            "Получить текст публичной веб-страницы. query — что искать на ней: "
            "с ним вернётся кусок вокруг совпадения, без него начало страницы.",
            "web.fetch",
            {
                "url": {"type": "string", "format": "uri"},
                "query": {"type": "string", "description": "что искать на странице"},
            },
            ["url"],
            risk="observe",
        )
        spec(
            "web_research",
            "Поиск и чтение нескольких публичных источников.",
            "web.research",
            {"query": {"type": "string"}, "max_sources": {"type": "integer", "minimum": 1, "maximum": 8}},
            ["query"],
            # `mutate`, а не `observe`: инструмент КЛАДЁТ найденные страницы в Raw
            # Object и во входящие (`_capture_web_sources`). Класс риска — это
            # обещание «после вызова ничего не останется», и оно здесь было
            # неправдой.
            #
            # Подтверждения человеком это НЕ добавляет: заявка требуется только для
            # `high`. Что добавляет — запись о начале вызова ДО эффекта, и именно её
            # тут не хватало: обрыв посреди записи оставлял след в базе и ноль
            # записей о вызове.
            risk="mutate",
        )
        spec(
            "entity_lookup",
            "Карточка сущности (человек, проект, часть, организация): связанные "
            "документы, теги по этим документам, диапазон дат документов, "
            "подтверждённые связи и число связей, ожидающих проверки человеком. "
            "Используй для «расскажи про проект X», «что известно об Y». Если "
            "спрашивают о ПРОШЛОМ («кто командовал в 2024», «где он служил "
            "тогда»), передай дату в `as_of` — вернётся картина на тот день, а "
            "не сегодняшняя. `known_at` — другой transaction-time срез: точный RFC3339-момент с "
            "часовым поясом, к которому Пятница уже успела узнать эти связи. "
            "Не подменяй им valid-time дату `as_of`.",
            "kg.read",
            {
                "name": {"type": "string"},
                "as_of": {
                    "type": "string",
                    "description": "Дата ГГГГ-ММ-ДД: показать связи, верные на неё",
                },
                "known_at": {
                    "type": "string",
                    "format": "date-time",
                    "description": (
                        "Transaction-time RFC3339 с UTC offset: показать только связи, "
                        "уже известные Пятнице к этому моменту"
                    ),
                },
            },
            ["name"],
            risk="observe",
        )
        spec(
            "data_sources",
            "Какие ВНЕШНИЕ базы подключены и о чём они. Зови первым, если вопрос про "
            "данные, которых нет в архиве: кадры, склад, заявки, любая рабочая система.",
            "data.read",
            {},
            [],
            risk="observe",
        )
        spec(
            "data_schema",
            "Таблицы и столбцы внешней базы. Зови ПЕРЕД `data_query`: без схемы имя "
            "таблицы приходится угадывать, а угаданное имя даёт не пустой ответ, а ошибку.",
            "data.read",
            {"source": {"type": "string", "description": "Имя источника из `data_sources`"}},
            ["source"],
            risk="observe",
        )
        spec(
            "data_query",
            "Одно чтение во внешней базе: ровно один SELECT, без точки с запятой. Запись "
            "запрещена структурно — не пытайся. Ответ содержит и строки, и сам запрос: "
            "покажи человеку, ЧТО именно ты спросил в чужой системе. Строк отдаётся не "
            "больше двухсот, и если обрезано, это сказано в ответе — не выдавай кусок за всё.",
            "data.read",
            {
                "source": {"type": "string", "description": "Имя источника из `data_sources`"},
                "sql": {"type": "string", "description": "Один SELECT по схеме из `data_schema`"},
            },
            ["source", "sql"],
            risk="observe",
        )
        spec(
            "relation_end",
            "Связь КОНЧИЛАСЬ: человек говорит «он перевёлся», «она там больше не "
            "работает», «этого больше нет». Отмечает связь оконченной, но НЕ стирает "
            "её: прошлое остаётся правдой о прошлом, и вопрос «как было тогда» на неё "
            "по-прежнему отвечается. Если между объектами несколько типов, инструмент "
            "ничего не меняет и возвращает candidates: повтори вызов с `relation_type`. "
            "Дату конца, если названа, передай в `valid_to`.",
            "kg.write",
            {
                "source": {"type": "string", "description": "Имя объекта, от которого связь"},
                "target": {"type": "string", "description": "Имя объекта, к которому связь"},
                "relation_type": {
                    "type": "string",
                    "enum": [item.value for item in RelationType],
                    "description": "Тип из candidates, если между объектами несколько связей",
                },
                "valid_to": {"type": "string", "description": "Дата конца ГГГГ-ММ-ДД, если названа"},
                "reason": {"type": "string", "description": "Чем это сказано, дословно"},
            },
            ["source", "target"],
            risk="mutate",
        )
        spec(
            "entity_create",
            "Предложить новую сущность через Inbox; не меняет граф автоматически.",
            "kg.write",
            {
                "name": {"type": "string"},
                "entity_type": {"type": "string", "enum": [item.value for item in EntityType]},
                "description": {"type": "string"},
                "aliases": {"type": "array", "items": {"type": "string"}},
            },
            ["name", "entity_type"],
            risk="mutate",
        )
        spec(
            "entity_link",
            "Предложить отношение между сущностями для review; не подтверждает его автоматически.",
            "kg.write",
            {
                "source_entity_id": {"type": "string"},
                "target_entity_id": {"type": "string"},
                "relation_type": {"type": "string", "enum": [item.value for item in RelationType]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "evidence": {"type": "string"},
            },
            ["source_entity_id", "target_entity_id", "relation_type"],
            risk="mutate",
        )
        spec("kg_stats", "Статистика личного графа знаний.", "kg.read", {}, [], "observe")
        spec(
            "make_file",
            "Собрать готовый файл и отправить человеку: Word (docx), Excel (xlsx), PDF "
            "или картинку (png). Используй, когда просят «сделай отчёт», «оформи в word», "
            "«выгрузи таблицей», «пришли файлом». Содержимое передаёшь структурой блоков — "
            "заголовки, абзацы, списки, таблицы; формат вёрстки тебя не касается. "
            "Пиши только то, что подтверждено собранными основаниями: файл выглядит "
            "весомее реплики в чате, и выдумка в нём живёт дольше.",
            "knowledge.read",
            {
                "kind": {"type": "string", "enum": sorted(SUPPORTED_KINDS)},
                "title": {"type": "string", "description": "Заголовок документа и имя файла."},
                "subtitle": {"type": "string", "description": "Подзаголовок: дата, основание, автор."},
                "blocks": {
                    "type": "array",
                    "description": (
                        "Содержимое по порядку. Каждый блок — объект: "
                        '{"kind":"heading","text":"..."} — раздел; '
                        '{"kind":"text","text":"..."} — абзац; '
                        '{"kind":"bullets","items":["...","..."]} — список; '
                        '{"kind":"table","rows":[["шапка","шапка"],["ячейка","ячейка"]]} — '
                        "таблица, первая строка считается шапкой."
                    ),
                    "items": {"type": "object"},
                },
                "filename": {"type": "string", "description": "Имя файла, если нужно своё."},
            },
            ["kind", "title", "blocks"],
            risk="observe",
        )
        spec(
            "collect_files",
            "Собрать ПРИШЕДШИЕ файлы в один архив (zip) и отправить человеку. Используй, "
            "когда просят собрать, выгрузить или прислать документы за какие-то дни: "
            "«собери документы за 10, 13 и 25 число», «скинь всё, что приходило вчера "
            "архивом», «выгрузи файлы за 29 июля». Кладутся ИСХОДНЫЕ файлы как есть. "
            "Не путай с make_file: тот сочиняет новый документ, этот пакует уже "
            "имеющиеся.",
            "knowledge.read",
            {
                "days": {
                    "type": "array",
                    "description": (
                        "Дни, за которые собирать. Каждый — либо полная дата «2026-07-29», "
                        "либо число месяца «25» (означает последнее такое число, которое "
                        "уже наступило). Перечисляй ровно те дни, что назвал человек: «за "
                        "10, 13 и 25» — это три дня, а не отрезок между ними."
                    ),
                    "items": {"type": "string"},
                },
                "name": {"type": "string", "description": "Имя архива, если нужно своё."},
            },
            ["days"],
            risk="observe",
        )
        spec(
            "remind",
            "Поставить напоминание. Используй, когда человек просит напомнить, не "
            "забыть, разбудить, предупредить: «напомни завтра в 15:00 про совещание», "
            "«не дай забыть про отчёт в пятницу», «напомни через неделю позвонить». "
            "НЕ ищи такие просьбы в архиве: человек не спрашивает, что там записано, "
            "он просит запомнить на будущее. Напоминание придёт в чат в назначенный "
            "день.",
            "kg.write",
            {
                "what": {
                    "type": "string",
                    "description": "О чём напомнить, словами человека: «совещание по поверке».",
                },
                "when": {
                    "type": "string",
                    "description": (
                        "Когда. Передавай ТАК ЖЕ, как сказал человек: «завтра», «завтра в "
                        "15:00», «3 августа», «в понедельник», «через неделю». Год не "
                        "дописывай, если его не назвали."
                    ),
                },
            },
            ["what", "when"],
            # Меняет данные: в графе появляется событие, по которому орган
            # напоминаний потом напишет человеку. Не «наблюдение», хотя и
            # безобидно — класс риска должен отвечать на вопрос «что останется
            # после вызова», а не «страшно ли это».
            risk="mutate",
        )
        spec(
            "upcoming",
            "Что человеку ПРЕДСТОИТ: поставленные напоминания и события с датами "
            "впереди. Используй на вопросы «какие планы», «что у меня сегодня», "
            "«что на неделе», «что предстоит», «о чём я просил напомнить». Это НЕ "
            "what_happened: тот смотрит назад, в уже случившееся, и на вопрос о "
            "планах пересказывает старую переписку.",
            "kg.read",
            {
                "days": {
                    "type": "integer",
                    "description": "На сколько дней вперёд смотреть. По умолчанию 7.",
                },
                "since": {
                    "type": "string",
                    "description": "Точная начальная ISO-дата YYYY-MM-DD; используется вместе с until.",
                },
                "until": {
                    "type": "string",
                    "description": "Точная конечная ISO-дата YYYY-MM-DD, не дальше 60 дней от since.",
                },
            },
            [],
            risk="observe",
        )
        spec(
            "what_happened",
            "Что происходило в названный момент или промежуток: сообщения переписки и "
            "документы, появившиеся в архиве, одной лентой по времени. Используй для "
            "вопросов «что было 26 июля в 15 часов», «что происходило вчера», «покажи "
            "события за прошлую неделю». НЕ путать с memory_search: тот ищет слова, а "
            "здесь спрашивают о моменте — по словам «26 июля» найдутся документы, где эта "
            "дата упомянута, а не то, что было в тот день.",
            "knowledge.read",
            {
                "since": {
                    "type": "string",
                    "description": (
                        "Начало промежутка. Передавай ТАК ЖЕ, как сказал человек: «29 июля», "
                        "«вчера», «позавчера», «3 дня назад», «26 июля 2026 в 15 часов», "
                        "«2026-07-26 15:00». Если человек НЕ назвал год — не добавляй его: "
                        "будет взят текущий. Дописанный по памяти год промахивается мимо архива."
                    ),
                },
                "until": {
                    "type": "string",
                    "description": "Конец промежутка. Без него берётся тот же момент, что и since: "
                    "названный час означает час целиком, названный день — день целиком.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            ["since"],
            risk="observe",
        )
        spec(
            "list_tags",
            "Список всех тегов личной базы знаний с числом записей у каждого. "
            "Используй для вопросов «какие у меня теги», «сгруппируй по тегам» — "
            "не путать с поиском по содержимому (memory_search).",
            "knowledge.read",
            {},
            [],
            risk="observe",
        )
        spec(
            "speak",
            "Озвучить текст голосом в дополнение к письменному ответу. Звать ТОЛЬКО когда "
            "пользователь явно попросил ответить/озвучить голосом в этом сообщении — не по умолчанию.",
            "tts.use",
            {"text": {"type": "string", "description": "Текст для озвучивания, обычно сам ответ."}},
            ["text"],
            risk="mutate",
        )
        spec(
            "resolve_duplicates",
            "Предложить возможные дубликаты без автоматического слияния.",
            "kg.merge",
            {},
            [],
            # `mutate`: обход НАПОЛНЯЕТ очередь слияний — это строки в базе, которые
            # человеку потом разбирать. «Без автоматического слияния» в описании
            # верно и относится к слиянию, а не к записи.
            risk="mutate",
        )
        spec(
            "conflict_list",
            "Показать порцию конфликтов знаний, ждущих решения человека. "
            "count — сколько в этой порции, total — сколько всего suggested. "
            "Решённые сюда не попадают. Дальше — conflict_decide по id.",
            "knowledge.read",
            {"limit": {"type": "integer", "minimum": 1, "maximum": 10}},
            [],
            risk="observe",
        )
        spec(
            "conflict_decide",
            "Решить один конфликт знаний. decision=dismiss — это не конфликт, обе "
            "записи остаются; keep_a / keep_b — оставить указанную сторону, вторая "
            "помечается устаревшей. Работает только со статусом suggested.",
            "knowledge.edit",
            {
                "conflict_id": {"type": "string"},
                "decision": {
                    "type": "string",
                    "enum": ["dismiss", "keep_a", "keep_b"],
                },
            },
            ["conflict_id", "decision"],
            risk="high",
        )
        spec(
            "entity_merge_decide",
            "Принять или отклонить одно предложение объединить сущности. "
            "accept переносит связи на цель; reject помечает пару «не дубликат» "
            "и она больше не предлагается. Список — через resolve_duplicates.",
            "kg.merge",
            {
                "candidate_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["accept", "reject"]},
                "target_entity_id": {
                    "type": "string",
                    "description": "какую из двух оставить; без неё — более богатая",
                },
            },
            ["candidate_id", "decision"],
            risk="high",
        )
        spec(
            "entity_merge_undo",
            "Откатить одно уже принятое слияние сущностей по id записи истории. "
            "Обе сущности и их связи с документами возвращаются к состоянию до "
            "слияния, в том числе когда у них были общие документы. Список "
            "недавних слияний — через HTTP /api/kg/merges или list_merge_history. "
            "Повторный откат той же записи запрещён.",
            "kg.merge",
            {"merge_id": {"type": "string"}},
            ["merge_id"],
            risk="mutate",
        )
        spec(
            "user_activity",
            "Что конкретный пользователь писал и загружал и когда. Имя можно указывать "
            "как обычно — «Иван», «у Ивана», с опечаткой или в другой раскладке. "
            "Требует прав администратора; чтение чужого аккаунта записывается в аудит. "
            "Само написанное показывается только полному администратору.",
            "admin.activity.read",
            {
                "person": {"type": "string"},
                "since": {"type": "string"},
                "until": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                # `additionalProperties: False` on the spec means an invented
                # argument is rejected outright, so the vocabulary has to be
                # declared — and declared as an enum, or the model fills the field
                # with plausible analysis names that silently return nothing.
                # `topics` = о чём пишет, `rhythm` = когда работает,
                # `volume` = сколько, `change` = что изменилось (требует `since`).
                "analysis": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(ANALYSES)},
                },
                "top": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            ["person"],
            risk="observe",
        )
        spec(
            "user_knowledge_search",
            "Найти по существу в записях конкретного человека и ответить по ним. "
            "Имя можно указывать как обычно — «Иван», «у Ивана», с опечаткой или в "
            "другой раскладке. В отличие от user_activity показывает НАПИСАННОЕ, а не "
            "объём, поэтому требует прав полного администратора; чтение чужого "
            "аккаунта записывается в аудит вместе с самим вопросом. Если в ответе есть "
            "filtered_out — столько записей нашлось, но по оценке они не отвечают.",
            # Верхнее право, а не `admin.activity.read`: здесь любой результат —
            # содержимое, метаданного уровня у этого инструмента не бывает.
            "admin.all_data.read",
            {
                "person": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            ["person", "query"],
            risk="observe",
        )
        spec(
            "inbox_list",
            "Показать элементы входящих знаний.",
            "inbox.read",
            {"status": {"type": "string", "enum": [item.value for item in InboxStatus]}},
            [],
            risk="observe",
        )
        spec(
            "code_run",
            "Запустить Python в ограниченном subprocess (не является OS-песочницей).",
            "code.run",
            {"code": {"type": "string"}},
            ["code"],
            risk="high",
        )
        spec(
            "mission_propose",
            "Предложить многошаговую миссию. Она не начинает выполняться по факту создания; "
            "будет ли она запущена сама или дождётся решения пользователя, зависит от "
            "настроек автономии — не обещай пользователю, что ничего не произойдёт без него.",
            "missions.create",
            {"goal": {"type": "string"}},
            ["goal"],
            risk="mutate",
        )
        spec(
            "mission_compensation",
            "Закрыть оборвавшийся шаг миссии после того, как человек разобрался с "
            "побочным эффектом сам. Не вызывается моделью напрямую: сюда приходят "
            "только по подтверждённой заявке.",
            "missions.control",
            {
                "mission_id": {"type": "string"},
                "task_id": {"type": "string"},
                "compensation": {"type": "string"},
                "checkpoint": {"type": "string"},
            },
            ["mission_id", "task_id"],
            risk="high",
        )


_ArchiveReader = Callable[
    [dict[str, Any], int],
    tuple[bytes | None, str, str] | None,
]


def _pack_authorized_archive(
    storage: Any,
    root: Path,
    rows: list[dict[str, Any]],
    name: str,
    *,
    user_id: str,
    days: list[str],
    utc_offset_minutes: int,
) -> tuple[bytes, list[str], int, int, int]:
    """Revalidate every stale list row and consume all bytes in one DB unit."""

    authorized_count = 0
    with storage.transaction() as conn:
        # This count and every file read share the same privacy snapshot.  A
        # reminder quarantine cannot land between them and leave stale totals or
        # names beside an archive assembled from a different authorization state.
        total = storage.count_files_received_on(
            user_id,
            days=days,
            utc_offset_minutes=utc_offset_minutes,
        )

        def authorized_reader(
            row: dict[str, Any],
            remaining_bytes: int,
        ) -> tuple[bytes | None, str, str] | None:
            nonlocal authorized_count
            raw_id = row.get("raw_id")
            if not isinstance(raw_id, str) or not raw_id:
                return None
            try:
                stored = read_authorized_file_in_transaction(
                    conn,
                    root,
                    raw_id,
                    user_id,
                    max_bytes=remaining_bytes,
                )
            except FileRecordUnavailable:
                # The stale listing is not an authority.  Even its old filename
                # must not be reflected in ``left_out`` after quarantine.
                return None
            except AuthorizedFileReadError as exc:
                authorized_count += 1
                return None, exc.filename, exc.reason
            authorized_count += 1
            return stored.content, stored.filename, ""

        packed, skipped, size = _pack_archive(
            root,
            rows,
            name,
            _authorized_reader=authorized_reader,
        )
    packed_count = authorized_count - len(skipped)
    # ``ZipFile`` emits a non-empty empty-container header.  Do not mistake that
    # for a successful collection when every stale row became private.
    if authorized_count == 0:
        packed = b""
    return packed, skipped, size, total, packed_count


def _pack_archive(
    root: Path,
    rows: list[dict[str, Any]],
    name: str,
    *,
    _authorized_reader: _ArchiveReader | None = None,
) -> tuple[bytes, list[str], int]:
    """Сложить исходные файлы в zip. Возврат: (архив, что не вошло, размер).

    Синхронная и блокирующая: вызывается через `run_blocking`, потому что читает
    сотни файлов с диска, а делать это в цикле событий значит подвесить всех
    остальных собеседников на время сборки.

    Файлы кладутся под ЧЕЛОВЕЧЕСКИМИ именами, а не под хешами хранилища:
    `dded8fc9….ogg` в архиве бесполезен. Совпадения имён разводятся номером —
    иначе второй `Отчёт.docx` молча затёр бы первый.
    """
    del name
    buffer = io.BytesIO()
    # Корень раскрывается ОДИН раз: `resolve()` ходит в файловую систему, а
    # сравнение делается на каждый файл из трёхсот.
    base_root = root.resolve()
    left_out: list[str] = []
    used: set[str] = set()
    size = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            if _authorized_reader is not None:
                loaded = _authorized_reader(row, _MAX_ARCHIVE_BYTES - size)
                if loaded is None:
                    continue
                payload, current_filename, failure = loaded
                if payload is None:
                    left_out.append(f"{current_filename} — {failure}")
                    continue
                fallback_name = current_filename or "file"
            else:
                stored = str(row.get("stored_path") or "")
                source = (root / stored).resolve()
                # Путь пришёл из базы, но проверяется всё равно: запись могла быть
                # сделана иначе, а `..` в ней увёл бы чтение за пределы хранилища.
                #
                # `is_relative_to`, а не сравнение начала строки. Разбор Сола
                # 2026-08-03 и проверка: при хранилище `/data/files` путь
                # `/data/files_backup/secret.pdf` проходит `startswith` — соседний
                # каталог, чьё имя начинается так же, границей не отделён вовсе.
                if not source.is_relative_to(base_root) or not source.is_file():
                    left_out.append(f"{row.get('filename') or row.get('title') or stored} — файла нет")
                    continue
                # Размер спрашивается У ФАЙЛОВОЙ СИСТЕМЫ, а не после чтения.
                #
                # Прежде файл читался целиком и лишь потом сверялся с потолком: файл
                # на несколько гигабайт сначала оказывался в памяти и только затем
                # объявлялся не поместившимся. Потолок стоял, но защищал он архив, а
                # не машину.
                try:
                    on_disk = source.stat().st_size
                except OSError as error:
                    left_out.append(f"{row.get('filename') or stored} — не прочитался ({error.errno})")
                    continue
                if size + on_disk > _MAX_ARCHIVE_BYTES:
                    left_out.append(f"{row.get('filename') or stored} — не поместился по размеру")
                    continue
                try:
                    payload = source.read_bytes()
                except OSError as error:
                    left_out.append(f"{row.get('filename') or stored} — не прочитался ({error.errno})")
                    continue
                fallback_name = source.name
                current_filename = str(row.get("filename") or row.get("title") or fallback_name)
            base = current_filename.strip() or fallback_name
            base = base.replace("/", "_").replace("\\", "_").lstrip(".") or fallback_name
            entry = base
            counter = 2
            while entry.casefold() in used:
                stem, dot, suffix = base.rpartition(".")
                entry = f"{stem} ({counter}){dot}{suffix}" if dot else f"{base} ({counter})"
                counter += 1
            used.add(entry.casefold())
            archive.writestr(entry, payload)
            size += len(payload)
    return buffer.getvalue(), left_out, size


def _safe_filename(title: str, extension: str) -> str:
    """Имя файла из заголовка — без путей и служебных знаков.

    Заголовок пишет модель, а он становится именем на диске и в Telegram: слэш
    или `..` в нём — это уже не косметика.
    """
    cleaned = "".join(char if char.isalnum() or char in " -_()" else " " for char in str(title or "").strip())
    cleaned = " ".join(cleaned.split())[:80].strip() or "Отчёт"
    return f"{cleaned}.{extension}"
