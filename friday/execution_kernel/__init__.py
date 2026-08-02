"""Capability-gated tool execution for the Friday agent runtime."""

from __future__ import annotations

import asyncio
import base64
import calendar
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from friday.people import resolve_person, unambiguous
from friday.permissions import ActorContext, AuthorizationError, AuthorizationService, current_actor
from friday.reports import SUPPORTED_KINDS, render, spec_from_payload
from friday.retrieval import best_snippet
from friday.storage._core import iso_date
from friday.storage._oversight import ANALYSES
from friday.storage.models import AuditEntry, EntityType, InboxStatus, RelationType, new_id
from friday.tts import TTSUnavailable, synthesize_speech
from friday.web_surfer import AllProvidersRefusedError
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

LOGGER = logging.getLogger(__name__)
Handler = Callable[..., Awaitable[dict[str, Any]]]


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
_DAY_MONTH_RE = re.compile(
    r"^(\d{1,2})(?:\s*-?\s*(?:го|е|ое))?\s+([а-яё]+)(?:\s+(\d{4}))?$", re.IGNORECASE
)
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
    month = next(
        (number for prefix, number in _MONTHS_RU.items() if month_word.startswith(prefix)), 0
    )
    if not month:
        return None
    year = int(year_text) if year_text else today.year
    try:
        return date(year, month, int(day)).isoformat()
    except ValueError:
        return None


#: Час в словах человека: «в 15:00», «в 15 часов», «к 9». Минуты необязательны.
_CLOCK_IN_TEXT = re.compile(
    r"(?:^|\D)(?:в|к|на)?\s*(?P<hour>[01]?\d|2[0-3])[:.](?P<minute>[0-5]\d)"
    r"|(?:^|\D)(?:в|к)\s*(?P<hour2>[01]?\d|2[0-3])\s*час",
    re.IGNORECASE,
)


#: Дни недели по-русски → номер в неделе, как их считает `date.weekday()`.
_WEEKDAY_NUMBERS = (
    ("понедельник", 0), ("вторник", 1), ("сред", 2), ("четверг", 3),
    ("пятниц", 4), ("суббот", 5), ("воскресен", 6),
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
    through = re.search(r"через\s+(\d+|неделю|день|дня|дней|месяц)\s*(день|дня|дней|недел\w*|месяц\w*)?", lowered)
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
POSTCONDITIONS: dict[str, Callable[[Any, str, dict[str, Any]], tuple[bool, str]]] = {
    "entity_merge_decide": _merge_postcondition,
    "conflict_decide": _conflict_postcondition,
}


# Какие вызовы модели не исполняются без человека. Ключ — имя инструмента,
# значение — предикат по аргументам, потому что риск живёт в аргументах, а не в
# инструменте: `entity_merge_decide` с `decision=reject` безопасен, с `accept` —
# нет. Спека v3 §5: модель предлагает, служба авторизует и исполняет.
HIGH_RISK_TOOLS: dict[str, Callable[[dict[str, Any]], bool]] = {
    "entity_merge_decide": _merge_needs_a_person,
    "conflict_decide": _conflict_needs_a_person,
    "code_run": lambda _arguments: True,
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

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
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
        return f"Результат {self.tool_name}:\n{encoded}"


_LLM_TOOL_PAYLOAD_MAX_CHARS = 11_900
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


# Длина выдержки в ответе инструмента. Десять результатов по 600 знаков — это 6 000
# плюс обвязка, то есть половина бюджета в 12 000: остаётся место и на ответ модели, и
# на второй вызов. Прежде сюда уходили тела документов, и одного среднего (16 565
# знаков на этом архиве) хватало, чтобы переполнить бюджет целиком.
_TOOL_EXCERPT_CHARS = 600


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
            "memory_search": self._memory_search,
            "message_search": self._message_search,
            "memory_save": self._memory_save,
            "web_search": self._web_search,
            "web_fetch": self._web_fetch,
            "web_research": self._web_research,
            "entity_lookup": self._entity_lookup,
            "entity_create": self._entity_create,
            "entity_link": self._entity_link,
            "kg_stats": self._kg_stats,
            "make_file": self._make_file,
            "what_happened": self._what_happened,
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

    def register(self, tool: ToolSpec) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def get_tool_names(self, actor: ActorContext | None = None) -> list[str]:
        return [tool.name for tool in self._visible_tools(actor)]

    def get_tool_definitions(self, actor: ActorContext | None = None) -> list[dict[str, Any]]:
        return [tool.to_openai() for tool in self._visible_tools(actor)]

    def _visible_tools(self, actor: ActorContext | None) -> list[ToolSpec]:
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
        try:
            self.authorization.require(actor, tool.security_id)
        except AuthorizationError as exc:
            await self._audit(actor, name, False, "authorization_denied", details=details)
            return ToolResult(name, False, error=str(exc))
        if name == "code_run" and not (self.settings and self.settings.code_execution_enabled):
            await self._audit(actor, name, False, "disabled", details=details)
            return ToolResult(name, False, error="Code execution is disabled by configuration")

        needs_person = HIGH_RISK_TOOLS.get(name)
        if needs_person and needs_person(arguments or {}):
            return await self._request_approval(actor, name, arguments or {}, details)

        timeout = 30
        if self.settings:
            timeout = max(1, self.settings.code_execution_timeout_sec if name == "code_run" else 30)
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
            await self._audit(actor, name, False, "timeout", details=details)
            return ToolResult(name, False, error="Tool execution timed out")
        except (TypeError, ValueError) as exc:
            await self._audit(actor, name, False, "invalid_arguments", details=details)
            return ToolResult(name, False, error=f"Invalid tool arguments: {exc}")
        except Exception as exc:
            LOGGER.exception("Tool %s failed", name)
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
                requested_by=actor.identity_id or actor.user_id,
                policy_epoch=str(actor.policy_epoch),
            )
        except Exception as exc:  # noqa: BLE001 - отказ в заявке не должен выполнять действие
            LOGGER.exception("Could not create an approval request for %s", name)
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
            chat_id = resolve_chat_id(storage, actor.user_id)
            if not chat_id or not self.settings:
                return
            if not may_push_to(self.settings, storage, actor.user_id, chat_id):
                return
            storage.enqueue_notification(
                actor.user_id,
                chat_id,
                f"Нужно ваше решение: {approval['summary']}\n\nОткрыть: /approvals",
                kind="approval",
                dedup_key=f"approval:{approval['id']}",
            )
        except Exception:  # noqa: BLE001 - недоставленное уведомление не отменяет заявку
            LOGGER.warning("Could not queue an approval notification", exc_info=True)

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
            candidate = None
            with suppress(Exception):
                candidate = storage.get_resolution_candidate(
                    str(arguments.get("candidate_id") or ""), user_id
                )
            if candidate:
                left = right = None
                with suppress(Exception):
                    left = storage.get_entity(str(candidate.get("entity_a_id") or ""), user_id)
                    right = storage.get_entity(str(candidate.get("entity_b_id") or ""), user_id)
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
            decision = str(arguments.get("decision") or "")
            conflict = None
            with suppress(Exception):
                conflict = storage.get_knowledge_conflict(user_id, str(arguments.get("conflict_id") or ""))
            if conflict:
                keep_a = decision == "keep_a"
                winner = str(conflict.get("knowledge_a_title" if keep_a else "knowledge_b_title") or "")
                loser = str(conflict.get("knowledge_b_title" if keep_a else "knowledge_a_title") or "")
                return (
                    f"Признать верной запись «{winner[:70]}» и объявить устаревшей «{loser[:70]}»"
                )
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
        except AuthorizationError as exc:
            await self._audit(actor, name, False, "authorization_denied", details={"approval": approval_id})
            return ToolResult(name, False, error=str(exc))

        arguments = dict(record.get("payload") or {})
        claimed = await run_blocking(
            storage.claim_action_approval,
            approval_id,
            actor.user_id,
            payload=arguments,
            policy_epoch=str(actor.policy_epoch),
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
            LOGGER.exception("Approved tool %s failed", name)
            await run_blocking(
                storage.finish_action_approval,
                approval_id,
                actor.user_id,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            await self._audit(actor, name, False, type(exc).__name__, details=details)
            return ToolResult(name, False, error=f"Tool failed: {type(exc).__name__}")
        verifier = POSTCONDITIONS.get(name)
        if verifier is not None:
            try:
                verified, reason = await run_blocking(verifier, storage, actor.user_id, arguments)
            except Exception as exc:  # noqa: BLE001 - непроверенное не объявляется сделанным
                verified, reason = False, f"проверку не удалось выполнить: {type(exc).__name__}: {exc}"
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
            else:
                max_sources = args.get("max_sources")
                if isinstance(max_sources, int):
                    details["max_sources"] = max_sources
            return details
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
            created_by=f"agent:{actor.user_id}",
        )
        return {
            "mission_id": mission.get("id"),
            "status": mission.get("status"),
            "title": mission.get("title"),
            "task_count": mission.get("task_count"),
            "queued_for_review": mission.get("status") == "proposed",
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
        except Exception:  # noqa: BLE001 — кривое имя пояса не должно ронять ход
            LOGGER.warning("Unknown timezone %r in settings; falling back to machine zone", name[:60])
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
        today = datetime.now(self._zone()).date()
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
        clock = ""
        spoken_clock = _CLOCK_IN_TEXT.search(str(when or ""))
        if spoken_clock:
            hour = int(spoken_clock.group("hour") or spoken_clock.group("hour2") or 0)
            minute = int(spoken_clock.group("minute") or 0)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                clock = f"{hour:02d}:{minute:02d}"
        elif len(stamp) >= 16 and stamp[11:16] != "00:00":
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
        entity = knowledge_graph.create_entity(actor.user_id, text[:120], EntityType.EVENT)
        knowledge_graph.set_event_time(actor.user_id, entity["id"], occurred_at, source="reminder")
        # Час не теряется: он остаётся в названии, потому что напоминания
        # рассылаются по календарным дням — точное время человек прочитает в
        # тексте, а не пропустит из-за того, что система его выбросила.
        del storage
        return {
            "created": True,
            "what": text[:120],
            "on": occurred_at,
            "at": clock,
            "entity_id": entity["id"],
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
        zone = self._zone()
        since_utc = datetime.fromisoformat(start_local).replace(tzinfo=zone).astimezone(UTC)
        until_utc = datetime.fromisoformat(end_local).replace(tzinfo=zone).astimezone(UTC)
        if until_utc < since_utc:
            since_utc, until_utc = until_utc, since_utc
        events = await run_blocking(
            storage.what_happened,
            actor.user_id,
            since=since_utc.isoformat(),
            until=until_utc.isoformat(),
            limit=max(1, min(int(limit), 200)),
        )
        totals = await run_blocking(
            storage.count_what_happened,
            actor.user_id,
            since=since_utc.isoformat(),
            until=until_utc.isoformat(),
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
        return {
            "understood": True,
            "asked_about": {"since": start_local, "until": end_local, "timezone": str(zone)},
            "total": totals,
            "shown": len(events),
            "events": events,
        }

    async def _memory_search(
        self,
        *,
        actor: ActorContext,
        query: str,
        limit: int = 10,
        since: str | None = None,
        until: str | None = None,
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
        # Требуется ТОЛЬКО хранилище: поиск по своему архиву не зависит ни от веба, ни
        # от конвейера приёма, и общий `_require_services` отказывал бы там, где
        # отказывать не за что.
        storage = self.storage
        if storage is None:
            raise RuntimeError("Execution kernel storage is not initialized")
        limit = max(1, min(int(limit), 50))
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
        # Гибридный поиск, если он выдан: у инструмента был свой, на FTS-префиксе и
        # LIKE, без эмбеддингов и без морфологии. Замерено на живой базе: «поставка»
        # находит 0 документов, «поставк» — 2; «отчет» — 13, «отчёт» — 3. То есть
        # слово в именительном падеже — ровно так его напишет модель, переформулируя
        # вопрос, — давало честное «ничего не нашлось» при существующих документах, и
        # 1537 честных векторов на этом пути не участвовали.
        rows: list[dict[str, Any]]
        found: dict[str, Any] | None = None
        dropped = 0
        # Объявляется ДО ветвления: без поиска (ядро собирают и без него — тесты, CLI)
        # ветка `else` не задаёт стратегию вовсе, и чтение ниже роняло весь инструмент.
        strategy: Any = None
        if self.searcher is not None:
            found = await self.searcher.search(actor.user_id, query, limit=limit, since=since, until=until)
            rows = list(found.get("results") or [])
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
        payload: dict[str, Any] = {"count": len(results), "query": query}
        # «Показано 10» и «в архиве ровно 10» — разные утверждения, и модель без
        # этой строки делает второе из первого. Замерено на живом корпусе: на
        # вопрос «кто из Уфы» она называла ОДНОГО человека как полный ответ, тогда
        # как Уфу упоминают 29 документов. Число стоит рядом с `count` и ДО
        # `results` — ответ инструмента режется по хвосту, и всё, что должно
        # пережить обрезку, стоит первым.
        matched = found.get("matched_at_least") if isinstance(found, dict) else None
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
        hits ``messages`` (FTS) under the same tenant as every other self-path.
        """
        storage = self.storage
        if storage is None:
            raise RuntimeError("Execution kernel storage is not initialized")
        limit = max(1, min(int(limit), 50))
        conv = " ".join(str(conversation_id or "").split()).strip() or None
        rows = storage.search_messages(
            actor.user_id,
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
                "requested_by": actor.user_id,
                "review_boundary": "inbox",
            },
            suggestion_overrides={
                "title": title.strip(),
                "tags": tags or [],
                "importance": parsed_importance,
            },
        )

    async def _web_search(self, *, actor: ActorContext, query: str, max_results: int = 5) -> dict[str, Any]:
        del actor
        _, _, web, _ = self._require_services()
        try:
            results = await web.search(query, max_results=max(1, min(int(max_results), 10)))
        except AllProvidersRefusedError as exc:
            # Отказ поисковиков — не факт об интернете. Модель обязана увидеть
            # разницу, иначе она сообщит человеку «ничего не нашлось» о запросе,
            # который никто не искал, либо ответит из своей памяти как из архива.
            LOGGER.warning("Web search refused for %r: %s", query[:80], exc)
            return {
                "query": query,
                "results": [],
                "search_failed": True,
                "error": "Поисковые системы не ответили — это сбой доступа, а не отсутствие результатов.",
                "instruction": (
                    "Скажи человеку, что поиск в интернете сейчас недоступен, и НЕ отвечай "
                    "по памяти, выдавая это за найденное."
                ),
            }
        return {"query": query, "results": [item.to_dict() for item in results]}

    async def _web_fetch(self, *, actor: ActorContext, url: str, query: str = "") -> dict[str, Any]:
        del actor
        _, _, web, _ = self._require_services()
        # `query` необязателен и означает «что искать на странице»: с ним модель
        # получает кусок вокруг совпадения, без него — начало страницы.
        return (await web.fetch(url)).to_dict(query=query)

    async def _web_research(self, *, actor: ActorContext, query: str, max_sources: int = 3) -> dict[str, Any]:
        _, _, web, _ = self._require_services()
        report = await web.research(query, max_sources=max(1, min(int(max_sources), 8)))
        captured = await self._capture_web_sources(actor, query, report)
        return {**report, "captured": captured} if captured else report

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
                        # Запрос — часть провенанса: по нему видно, зачем эта
                        # страница вообще попала в архив.
                        "search_query": query,
                        **({"content_truncated": True} if source.get("truncated") else {}),
                    },
                )
            except Exception:  # noqa: BLE001 — сохранение не должно ронять ответ
                LOGGER.exception("Failed to capture web source %s", url[:120])
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

    async def _entity_lookup(self, *, actor: ActorContext, name: str) -> dict[str, Any]:
        _, kg, _, _ = self._require_services()
        # Через поток, как и HTTP-двойник этой же карточки: профиль на широкой
        # сущности — несколько SQL по 22 тысячам связей. Маршрут перевели на
        # `run_blocking`, а этот путь забыли — и он ХУЖЕ, потому что инструмент
        # зовётся внутри агентского цикла, где ждут все остальные разговоры.
        entity = await run_blocking(kg.find_entity, actor.user_id, name)
        if not entity:
            return {"found": False, "entity": None}
        profile = await run_blocking(kg.entity_profile, entity["id"], actor.user_id)
        return {"found": True, "entity": entity, **profile}

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
        items = storage.list_knowledge_tags(actor.user_id)
        return {"tags": items, "count": len(items)}

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
            LOGGER.warning("tts: unavailable (%s)", exc)
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
        except ValueError as exc:
            return {"created": False, "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 — вёрстка не должна ронять ход
            LOGGER.exception("Report rendering failed")
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

        storage, _, _, _ = self._require_services()
        page = max(1, min(int(limit), 10))
        items = await run_blocking(
            storage.list_knowledge_conflicts,
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
                compact.append(
                    {
                        "id": str(enriched.get("id") or ""),
                        "conflict_type": str(enriched.get("conflict_type") or ""),
                        "confidence": float(enriched.get("confidence") or 0.0),
                        "triage": enriched.get("triage") or {},
                        "a": {
                            "id": str(enriched.get("knowledge_a_id") or ""),
                            "title": str(enriched.get("knowledge_a_title") or "")[:200],
                            "summary": str(enriched.get("knowledge_a_summary") or "")[:400],
                        },
                        "b": {
                            "id": str(enriched.get("knowledge_b_id") or ""),
                            "title": str(enriched.get("knowledge_b_title") or "")[:200],
                            "summary": str(enriched.get("knowledge_b_summary") or "")[:400],
                        },
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
        choice = str(decision or "").casefold().strip()
        if choice not in {"dismiss", "keep_a", "keep_b"}:
            raise ValueError("decision must be dismiss, keep_a or keep_b")
        conflict = await run_blocking(kg.storage.get_knowledge_conflict, actor.user_id, conflict_id)
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
                reviewed_by=actor.user_id,
                resolution_note="telegram/agent: dismissed",
            )
            return {"status": "dismissed", "conflict_id": conflict_id, "item": result}
        winner_id = str(conflict["knowledge_a_id"]) if choice == "keep_a" else str(conflict["knowledge_b_id"])
        result = await run_blocking(
            kg.resolve_conflict,
            actor.user_id,
            conflict_id,
            winner_id,
            reviewed_by=actor.user_id,
            resolution_note=f"telegram/agent: {choice}",
        )
        return {"status": "resolved", "conflict_id": conflict_id, "winner_id": winner_id, "item": result}

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
                resolved_by=actor.user_id,
            )
            return {"status": "rejected", "candidate_id": candidate_id}
        merged = await run_blocking(
            kg.resolver.accept_resolution,
            candidate_id,
            actor.user_id,
            target_entity_id=target_entity_id,
            resolved_by=actor.user_id,
        )
        return {"status": "merged", "candidate_id": candidate_id, "result": merged}

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
            undone_by=actor.user_id,
        )
        return {"status": "undone", "merge_id": merge_id, "result": result}

    async def _inbox_list(self, *, actor: ActorContext, status: str | None = None) -> dict[str, Any]:
        storage, _, _, _ = self._require_services()
        status_value = InboxStatus(status) if status else None
        items = storage.list_inbox(actor.user_id, status_value, limit=20)
        # `count` — сколько ПОКАЗАНО, `total` — сколько есть. Возвращать длину среза
        # под именем count значит сказать модели «у вас 20 входящих» при двухстах, а
        # модель перескажет это человеку прозой, где оговорку уже не восстановить.
        # Соседний инструмент в этом же файле решает ровно эту задачу явно.
        return {
            "items": items,
            "count": len(items),
            "total": storage.count_inbox(actor.user_id, status_value),
            "truncated": len(items) < storage.count_inbox(actor.user_id, status_value),
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
                    user_id=actor.user_id,
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

        storage.log_audit(
            AuditEntry(
                id=new_id("audit"),
                user_id=actor.user_id,
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
        answer: dict[str, Any] = {
            "resolved": chosen.to_dict(),
            "content": "full" if include_content else "redacted",
            "summary": storage.user_activity_summary(chosen.user_id, since=since, until=until),
            "items": storage.user_activity(
                chosen.user_id,
                since=since,
                until=until,
                limit=max(1, min(int(limit), 200)),
                include_content=include_content,
            ),
        }
        if analysis:
            try:
                answer["analysis"] = storage.user_activity_analysis(
                    chosen.user_id,
                    since=since,
                    until=until,
                    analyses=list(analysis),
                    top=max(1, min(int(top), 50)),
                    include_content=include_content,
                )
            except ValueError as exc:
                # The schema constrains the enum, so this is reachable only if the
                # vocabulary and the schema drift apart. Say which values exist
                # rather than failing the whole call.
                answer["analysis_error"] = str(exc)
        return answer

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
                    user_id=actor.user_id,
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

        # Clamp on both branches: execute() does not enforce JSON-schema max, so
        # the bare storage fallback used to honour an unbounded limit while
        # HybridSearcher stayed capped at 20. Same bound as the declared schema.
        clamped_limit = max(1, min(int(limit), 20))
        found = (
            await self.searcher.search(chosen.user_id, clean_query, limit=clamped_limit)
            if self.searcher is not None
            else {"results": storage.search_knowledge(chosen.user_id, clean_query, limit=clamped_limit)}
        )
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
                user_id=actor.user_id,
                action="tool.user_knowledge_search",
                target_type="user",
                target_id=chosen.user_id,
                after_json={
                    "asked_for": person[:200],
                    "match_method": chosen.method,
                    # Вопрос, а не только факт чтения: без него запись в журнале не
                    # позволяет понять, что именно искали в чужом корпусе.
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
        if dropped:
            answer["filtered_out"] = dropped
        answer["results"] = [
            {
                "id": str(row.get("id") or ""),
                "title": str(row.get("title") or "Без названия")[:200],
                "kind": str(row.get("knowledge_kind") or "note"),
                "updated_at": row.get("updated_at"),
                "excerpt": best_snippet(
                    clean_query, str(row.get("content") or ""), max_chars=_TOOL_EXCERPT_CHARS
                ),
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
            script.write_text(code, encoding="utf-8")
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
            "есть, но не в этом периоде — скажи именно так.",
            "search.use",
            {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "since": {"type": "string", "description": "ГГГГ-ММ-ДД, начало периода"},
                "until": {"type": "string", "description": "ГГГГ-ММ-ДД, конец периода"},
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
            "Поиск в открытом интернете.",
            "web.search",
            {"query": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 10}},
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
            risk="observe",
        )
        spec(
            "entity_lookup",
            "Карточка сущности (человек, проект, часть, организация): связанные "
            "документы, теги по этим документам, диапазон дат документов, "
            "подтверждённые связи и число связей, ожидающих проверки человеком. "
            "Используй для «расскажи про проект X», «что известно об Y».",
            "kg.read",
            {"name": {"type": "string"}},
            ["name"],
            risk="observe",
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
            risk="observe",
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


def _safe_filename(title: str, extension: str) -> str:
    """Имя файла из заголовка — без путей и служебных знаков.

    Заголовок пишет модель, а он становится именем на диске и в Telegram: слэш
    или `..` в нём — это уже не косметика.
    """
    cleaned = "".join(
        char if char.isalnum() or char in " -_()" else " " for char in str(title or "").strip()
    )
    cleaned = " ".join(cleaned.split())[:80].strip() or "Отчёт"
    return f"{cleaned}.{extension}"
