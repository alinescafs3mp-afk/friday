"""Supervised background enrichment and maintenance workers.

Workers iterate over every active tenant and never merge entities or classify
uncertain inbox items without a user decision.  Failures are isolated per task,
so one bad document or tenant cannot stop lifecycle, backup, or graph work.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from friday.config import FridaySettings
from friday.document_catalog.worker_state import (
    DOCUMENT_CATALOG_WORKER_STATE_KEY,
    DocumentCatalogTenantState,
    DocumentCatalogWorkerState,
    decode_document_catalog_worker_state,
    document_catalog_worker_tenant_key,
    encode_document_catalog_worker_state,
    load_document_catalog_worker_namespace_key,
)
from friday.retrieval import (
    chunk_scheme,
    knowledge_chunk_units,
    knowledge_search_text,
    pack_vector,
    unpack_vector,
)
from friday.storage.models import InboxStatus
from friday.workers._blocking import current_task, in_flight, run_blocking

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntervalTask:
    name: str
    func: Callable[[], Awaitable[Any]]
    interval_sec: float
    enabled: bool = True
    run_immediately: bool = True
    timeout_sec: float = 300.0


# Consecutive per-item failures that mean the model endpoint is down rather than the
# documents being bad. Three is enough to tell them apart without abandoning a batch
# over one malformed item.
#
# Различать их обязательно, и в живом журнале видно, почему. Из 1188 неудач советчика
# 1023 — «модель не вернула JSON» (беда объекта), 164 — обрыв связи (беда канала), а
# считались они одинаково: три подряд любых, и в журнал шло «модель недоступна», хотя
# модель отвечала. Ложный диагноз останавливал советы всему арендатору.
_ADVICE_ENDPOINT_DOWN_AFTER = 3
# Сколько раз пробовать один и тот же объект, прежде чем оставить его в покое.
#
# Памяти о неудачах не было вовсе, и очередь `pending` отсортирована по promotion_score:
# объект, на котором совет не выходит, стоял в голове и брался КАЖДЫЙ тик. В журнале
# живой установки одни и те же inbox_id падают 168, 156, 146, 138 и 138 раз, а всё, что
# за ними, не получало совета никогда. Доказано тестом: пять тиков, пятнадцать
# обращений — все на трёх падающих, до здорового не дошло ни одного.
#
# Объект при этом НЕ выбрасывается: он остаётся в очереди человека, просто без
# машинного совета. Отметка живёт в `suggestions_json` рядом с самим советом и
# обнуляется, как только совет получился.
_ADVICE_ITEM_ATTEMPTS = 3


def _advice_attempts(item: dict[str, Any]) -> int:
    """Сколько раз совет по этому объекту уже не получился."""
    raw = item.get("suggestions_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return 0
    if not isinstance(raw, dict):
        return 0
    try:
        return int(raw.get("model_advice_failures") or 0)
    except (TypeError, ValueError):
        return 0


def _is_endpoint_failure(error: BaseException) -> bool:
    """Отказал канал до модели, а не сам документ.

    Разделение взято из живого журнала, а не придумано: 1023 неудачи из 1188 —
    «модель не вернула JSON» и «ответ оборвался по бюджету токенов», то есть беда
    КОНКРЕТНОГО объекта; 164 — `httpcore.ReadError`, `RemoteProtocolError` и
    таймауты, то есть беда канала. Считать их вместе значит объявлять модель
    недоступной, пока она отвечает.

    Проверяется по дереву модулей исключения, а не по списку классов: httpx и
    httpcore поднимают десяток разных типов, и перечислять их — заводить второй
    список, который отстанет от библиотеки. Плюс `LLMUnavailableError`: роутер
    отдаёт им «модель выключена», «в ответе нет вариантов» и «не вышло после всех
    повторов» — это канал, но по типу исключения его иначе не отличить от беды
    документа, потому что обычный RuntimeError означает и то и другое.
    """
    from friday.agent_runtime.llm import LLMUnavailableError

    if isinstance(error, LLMUnavailableError | TimeoutError | ConnectionError | OSError):
        return True
    module = type(error).__module__.split(".", 1)[0]
    return module in {"httpx", "httpcore", "anyio", "ssl", "socket"}


# Ceiling on the single input that carries an object's whole-document vector. Generous
# next to a passage (default 1200) and far below what an embeddings service will refuse.
# Потолок, который сервис эмбеддингов кладёт на ОДИН вход. Замерено на живом
# эндпоинте: 40 000 символов дают `400 an input exceeds the 32768-character limit`,
# и падает весь запрос целиком, а не один вход.
#
# Здесь стояло предупреждение, что `_EMBED_REQUEST_MAX_CHARS` (40 000) выше этой
# границы и потому заманчив как образец для потолка одного входа. Оно протухло в
# тот же вечер: бюджет запроса стал 30 000, то есть НИЖЕ 32 768. Ловушки больше
# нет, но и ориентироваться на знаки не стоит — сервис считает токены, и оба
# настоящих предела (8192 на вход, 16384 на запрос) описаны ниже.
_EMBED_INPUT_MAX_CHARS = 32_768

# Замерено на настоящем корпусе владельца против живого эндпоинта: настоящий предел
# сервиса — 8192 ТОКЕНА на вход, а не символы, и плотность зависит от текста. На
# связной русской прозе выходит 2.86 символа на токен, но в таблицах, номерах и
# латинице вперемешку — заметно хуже:
#
#     потолок 20 000: отвергнуто 17 из 40 длинных документов
#     потолок 16 000: отвергнуто 15 из 46
#     потолок 14 000: отвергнуто  8 из 52
#     потолок 12 000: отвергнуто  5 из 60
#     потолок 10 000: отвергнуто  0 из 60
#
# Прежние 20 000 роняли КАЖДУЮ пачку из 64 объектов: одного негодного входа хватает,
# чтобы сервис отверг весь запрос, и фоновая индексация стояла намертво, повторяя
# один и тот же отказ каждые две минуты.
_DOC_VECTOR_MAX_CHARS = 10_000

# How much TEXT one embeddings request may carry. Batches were bounded by input COUNT
# alone, which says nothing about the work: 256 strings of eighteen characters and 65
# passages of two thousand differ by two orders of magnitude.
#
# The rate this was calibrated against — ~2800 characters/second — was measured on a
# quiet service. Re-measured under normal load it is **768 characters/second**, so a
# full 40000-character request takes **51.6 seconds**, not fifteen. That is why the
# request timeout now follows `llm_timeout_sec` instead of a hard 60 (see
# `EmbeddingBackend.embed`): the cap and the timeout are two halves of one bound, and
# they had drifted apart by a factor of three and a half.
#
# The cap stays where it is because it bounds MEMORY and latency per request, not
# only the timeout race. Re-measure both together if the model changes.
#
# Замерено 2026-07-29 против живого сервиса на видеокарте: у него ДВА предела, а
# не один, и второй здесь не учитывался.
#
#     один вход:  8192 токена  (подтверждено двоичным поиском: 28 664 знака)
#     весь запрос: 16384 токена и 262 144 знака
#
# Плотность на этом корпусе (60 документов, prompt_tokens от самого сервиса):
# медиана 2.65 знака на токен, худший случай 2.50. Значит запрос в 40 000 знаков
# весит около 16 000 токенов — на два процента ниже потолка, и всякий текст чуть
# плотнее среднего его перешагивает. Отсюда 206 отказов `request_too_many_tokens`
# за один досчёт, и каждый из них укорачивал ВСЕ тексты пачки вдвое: документы
# получали вектор по половине своего текста, молча.
#
# 30 000 знаков — это 16 384 токена при плотности 1.83, то есть запас в четверть
# к худшему замеренному значению. Потолок на ОДИН вход трогать нельзя: он выведен
# из отказов на настоящих документах (таблица выше) и говорит о другом пределе.
_EMBED_REQUEST_MAX_CHARS = 30_000

# How many objects one vault render page carries. Named so a test can shrink it:
# the page-boundary skew this loop had to stop having needs more than one page,
# and a test that never crosses a boundary passes on the broken code too.
_VAULT_PAGE = 250

# Reconcile and backfill share one item budget.  Both storage operations run in
# threads, so the supervisor timeout is only a last-resort health signal: it
# cannot cancel an already-running SQLite call.  A small page is the cooperative
# bound that keeps one minute of catalog maintenance from turning into a corpus
# scan.
_DOCUMENT_CATALOG_TICK_LIMIT = 128
_DOCUMENT_CATALOG_CURSOR_KEY = DOCUMENT_CATALOG_WORKER_STATE_KEY

# Ниже этого тик не считается нагрузкой и отдыха не назначает: пауза защищает
# внешний сервис, а не расписание.
_EMBED_REST_MIN_WORK_SEC = 1.0


def _document_catalog_phase_page(
    report: Any,
    *,
    spent_key: str,
    limit: int,
) -> tuple[int, bool, str | None]:
    """Authenticate one bounded storage page before checkpointing it."""

    if not isinstance(report, dict):
        raise ValueError("document catalog phase report is invalid")
    spent = report.get(spent_key)
    has_more = report.get("has_more")
    if type(spent) is not int or spent < 0 or spent > limit or type(has_more) is not bool:
        raise ValueError("document catalog phase report is out of bounds")
    next_cursor = report.get("next_after_raw_object_id")
    if has_more:
        if not isinstance(next_cursor, str):
            raise ValueError("document catalog phase did not return an exact TEXT cursor")
        return spent, True, next_cursor
    if next_cursor is not None:
        raise ValueError("exhausted document catalog phase returned a cursor")
    return spent, False, None


def _document_catalog_processed(report: Any, *, maximum: int) -> int:
    """Require a processed count that cannot exceed its examined phase page."""

    if not isinstance(report, dict):
        raise ValueError("document catalog phase report is invalid")
    processed = report.get("processed")
    if type(processed) is not int or processed < 0 or processed > maximum:
        raise ValueError("document catalog processed count is out of bounds")
    return processed


class WorkerBatchError(RuntimeError):
    """One task completed its tenant sweep with isolated failures."""


class _DocumentCatalogPhaseError(RuntimeError):
    """One tenant allocation completed with an isolated catalog phase failure."""


class WorkerSupervisor:
    def __init__(
        self,
        state_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._tasks: list[IntervalTask] = []
        self._running = False
        self._handles: list[asyncio.Task[Any]] = []
        self._states: dict[str, dict[str, Any]] = {}
        self._state_sink = state_sink
        # Health writes are queued and drained on a thread — see `_publish`.
        self._sink_queue: asyncio.Queue[tuple[str, dict[str, Any]]] | None = None
        self._sink_handle: asyncio.Task[None] | None = None

    def register(
        self,
        name: str,
        func: Callable[[], Awaitable[Any]],
        interval_sec: float,
        *,
        enabled: bool = True,
        run_immediately: bool = True,
        timeout_sec: float | None = None,
    ) -> None:
        normalized_interval = max(1.0, float(interval_sec))
        self._tasks.append(
            IntervalTask(
                name=name,
                func=func,
                interval_sec=normalized_interval,
                enabled=enabled,
                run_immediately=run_immediately,
                timeout_sec=max(
                    5.0,
                    float(timeout_sec if timeout_sec is not None else min(normalized_interval, 900.0)),
                ),
            )
        )

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {name: dict(state) for name, state in self._states.items()}

    @property
    def max_timeout_sec(self) -> float:
        """The longest a single task may hold a thread. Shutdown's drain budget.

        Derived rather than guessed: a constant shorter than this abandons work that
        is still legitimately running, and one longer than it just delays a shutdown
        that is already stuck.
        """
        return max((task.timeout_sec for task in self._tasks), default=0.0)

    def _publish(self, task: IntervalTask, **updates: Any) -> None:
        current = dict(self._states.get(task.name, {}))
        current.update(
            {
                "name": task.name,
                "interval_sec": task.interval_sec,
                "timeout_sec": task.timeout_sec,
                "enabled": task.enabled,
                **updates,
            }
        )
        self._states[task.name] = current
        if self._state_sink is None:
            return
        if self._sink_queue is not None:
            # Off the event loop, in order. The sink writes to SQLite, and every
            # write takes the process-wide write lock: measured with a worker
            # thread holding a 3-second transaction (the shape of one batch of
            # embedding vectors), this call blocked the loop for 3.00 seconds —
            # no HTTP request, no Telegram message, no other worker, until the
            # writer committed. `_publish` runs on the loop several times per task
            # run, so this is the health bookkeeping stalling the product.
            #
            # A queue rather than a task per write: these are heartbeats for the
            # same few keys, and out-of-order writes would leave a stale "running"
            # sitting on top of the "ok" that followed it.
            self._sink_queue.put_nowait((task.name, current))
            return
        try:  # no loop running (unit tests call `_publish` directly)
            self._state_sink(task.name, current)
        except Exception as exc:
            LOGGER.debug("Could not persist worker state for %s (%s)", task.name, type(exc).__name__)

    async def _drain_state_sink(self) -> None:
        assert self._sink_queue is not None
        while True:
            name, state = await self._sink_queue.get()
            try:
                if self._state_sink is not None:
                    await asyncio.to_thread(self._state_sink, name, state)
            except Exception as exc:
                LOGGER.debug("Could not persist worker state for %s (%s)", name, type(exc).__name__)
            finally:
                self._sink_queue.task_done()

    async def run(self) -> None:
        if self._running:
            return
        self._running = True
        if self._state_sink is not None:
            self._sink_queue = asyncio.Queue()
            self._sink_handle = asyncio.create_task(
                self._drain_state_sink(), name="jericho-worker-state-sink"
            )
        self._handles = [
            asyncio.create_task(self._run_task(task), name=f"jericho-worker:{task.name}")
            for task in self._tasks
            if task.enabled
        ]
        LOGGER.info("Worker supervisor started with %d tasks", len(self._handles))
        with suppress(asyncio.CancelledError):
            await asyncio.gather(*self._handles)

    async def stop(self) -> None:
        self._running = False
        for handle in self._handles:
            handle.cancel()
        await asyncio.gather(*self._handles, return_exceptions=True)
        self._handles.clear()
        if self._sink_handle is not None:
            # Let the queued health writes land before cancelling the drainer:
            # the last thing every task publishes is why it stopped, and losing
            # that turns a clean shutdown into a worker that looks stuck at
            # "running" forever.
            if self._sink_queue is not None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._sink_queue.join(), timeout=5.0)
            self._sink_handle.cancel()
            with suppress(asyncio.CancelledError):
                await self._sink_handle
            self._sink_handle = None
            self._sink_queue = None
        LOGGER.info("Worker supervisor stopped")

    @staticmethod
    def _seconds_since(value: Any) -> float | None:
        """Сколько прошло с записанного момента; None — если момента нет или он битый."""
        text = str(value or "").strip()
        if not text:
            return None
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return (datetime.now(UTC) - moment).total_seconds()

    def _initial_delay(self, task: IntervalTask) -> float:
        """Сколько ждать до первого прогона С УЧЁТОМ того, когда задача шла в прошлый раз.

        Расписание жило только в памяти процесса: задача с `run_immediately=False`
        всегда спала полный интервал от МОМЕНТА СТАРТА. Значит суточная задача не
        выполнялась НИ РАЗУ, если сервис перезапускают чаще раза в сутки.

        Не гипотеза. На живой установке за всё время нет ни одной записи `chronicle` и
        `reflection` в исходящих уведомлениях; `knowledge_quality_scan` (12 ч) последний
        раз отработал за 4.5 суток и девять пропущенных окон до проверки, а у второго
        арендатора не отрабатывал никогда. Тем же голоданием затронуты `retrieval_eval`
        (сутки) и `knowledge_dedup` (6 часов).

        Показательно, что `scheduled_backup` сделан иначе — с собственной отметкой
        последнего запуска — и копии реально идут. Здесь то же самое, но общим местом:
        фаза берётся из сохранённого состояния, а не начинается заново.
        """
        elapsed = self._seconds_since((self._states.get(task.name) or {}).get("last_finished_at"))
        if elapsed is None:
            # О задаче ничего не известно: первый запуск на этой установке. Ждём
            # интервал, как и раньше, — так задумано, чтобы старт не бил залпом.
            return task.interval_sec
        # Долг по расписанию есть — идём сразу, но не залпом: небольшая задержка
        # разводит задачи, проснувшиеся после одного перезапуска.
        return max(0.0, task.interval_sec - elapsed)

    def restore(self, states: dict[str, dict[str, Any]]) -> None:
        """Поднять прошлое состояние задач ДО запуска цикла.

        Без этого `self._states` пуст при старте, и происходят сразу две беды. Первая:
        `_initial_delay` не знает, когда задача шла в прошлый раз, и всегда ждёт полный
        интервал — суточная задача не выполняется никогда, если сервис перезапускают
        чаще раза в сутки. Вторая: первый же `_publish` затирает записанную историю
        (`last_success_at`, `last_finished_at`, число подряд идущих отказов), потому
        что состояние пишется в хранилище ЦЕЛИКОМ, а не полями.
        """
        for name, state in states.items():
            if isinstance(state, dict):
                self._states[name] = dict(state)

    async def _run_task(self, task: IntervalTask) -> None:
        if not task.run_immediately:
            delay = self._initial_delay(task)
            self._publish(
                task,
                status="scheduled",
                next_run_at=(datetime.now(UTC) + timedelta(seconds=delay)).isoformat(timespec="seconds"),
                # Счётчик отказов НЕ обнуляется: он жил только в памяти, и каждый
                # перезапуск стирал историю падений. Задача с интервалом 12 часов,
                # падающая каждый прогон, не могла добраться до порога деградации,
                # если машину перезагружают ежедневно.
                consecutive_failures=int(
                    (self._states.get(task.name) or {}).get("consecutive_failures") or 0
                ),
            )
            if delay > 0:
                await asyncio.sleep(delay)
        while self._running:
            started = time.monotonic()
            started_at = datetime.now(UTC)
            previous = self._states.get(task.name, {})
            self._publish(
                task,
                status="running",
                last_started_at=started_at.isoformat(timespec="seconds"),
                error_type=None,
                error_message=None,
                consecutive_failures=int(previous.get("consecutive_failures") or 0),
            )
            # A previous run that timed out may still have a thread writing. Starting
            # now would put two of the same task on one database; skipping costs a
            # cycle and says so, which is the honest outcome.
            stranded = in_flight(task.name)
            if stranded:
                LOGGER.warning(
                    "Worker %s skipped: %d blocking call(s) from a previous run still executing",
                    task.name,
                    stranded,
                )
                self._publish(
                    task,
                    status="skipped",
                    last_finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
                    error_type=None,
                    error_message="previous run still has blocking work in flight",
                )
                await asyncio.sleep(max(1.0, task.interval_sec))
                continue
            token = current_task.set(task.name)
            try:
                async with asyncio.timeout(task.timeout_sec):
                    await task.func()
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                elapsed = time.monotonic() - started
                failures = int(previous.get("consecutive_failures") or 0) + 1
                LOGGER.error("Worker %s exceeded %.1fs timeout", task.name, task.timeout_sec)
                self._publish(
                    task,
                    status="timeout",
                    last_finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
                    last_duration_sec=round(elapsed, 3),
                    error_type="TimeoutError",
                    error_message="worker exceeded its configured execution budget",
                    consecutive_failures=failures,
                )
            except Exception as exc:
                elapsed = time.monotonic() - started
                failures = int(previous.get("consecutive_failures") or 0) + 1
                LOGGER.error("Worker %s failed (%s)", task.name, type(exc).__name__)
                self._publish(
                    task,
                    status="error",
                    last_finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
                    last_duration_sec=round(elapsed, 3),
                    error_type=type(exc).__name__,
                    error_message="worker failed; inspect secure logs for details",
                    consecutive_failures=failures,
                )
            else:
                elapsed = time.monotonic() - started
                self._publish(
                    task,
                    status="ok",
                    last_finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
                    last_success_at=datetime.now(UTC).isoformat(timespec="seconds"),
                    last_duration_sec=round(elapsed, 3),
                    error_type=None,
                    error_message=None,
                    consecutive_failures=0,
                )
            finally:
                current_task.reset(token)
            elapsed = time.monotonic() - started
            delay = max(1.0, task.interval_sec - elapsed)
            self._publish(
                task,
                next_run_at=(datetime.now(UTC) + timedelta(seconds=delay)).isoformat(timespec="seconds"),
            )
            await asyncio.sleep(delay)


def _sync_vault_page(vault: Any, storage: Any, objects: Sequence[dict[str, Any]]) -> None:
    """Write one page of Knowledge Objects to the vault, off the event loop.

    Entity names come along so the note can carry `[[wikilinks]]`; they are fetched
    per page rather than per object so the vault sync does not become the N+1 that
    the graph layer just stopped being.
    """
    links = storage.list_knowledge_entity_links_for([str(item.get("id") or "") for item in objects])
    for item in objects:
        try:
            ko_id = str(item.get("id") or "")
            user_id = str(item.get("user_id") or "")
            # Re-read under the same BEGIN IMMEDIATE writer boundary held by
            # soft-delete/hard-purge.  Holding it through the durable filesystem
            # write closes the stale-page race: a projector write is ordered wholly
            # before a delete (which then removes it), or wholly after and observes
            # the object absent/deleted/private.  An autocommit recheck here would
            # leave a TOCTOU window before os.replace.
            with storage.transaction():
                current = storage.get_knowledge_object(ko_id, user_id)
                if current is None or current.get("deleted_at"):
                    continue
                vault.sync_object(
                    {
                        **current,
                        "_entity_names": links.get(ko_id, []),
                    }
                )
        except OSError:
            # One unwritable note must not take the rest of the vault with it. The
            # loop below this one paginates the whole tenant and ends in
            # `prune_orphans`, so an exception here used to freeze every later
            # object AND leave deleted notes on disk in plaintext indefinitely —
            # the exact thing pruning exists to prevent.
            LOGGER.warning("vault sync skipped one object")


def _prune_vault_against_final_snapshot(
    vault: Any,
    storage: Any,
    user_id: str,
    live_at_start: set[str],
) -> int:
    """Prune against objects public at both ends of the render sweep.

    The start snapshot protects live notes from pagination reorder.  It cannot,
    by itself, authorize retention: a KO can be deleted or become private while
    pages are rendered.  Re-read the privacy-filtered ids immediately before the
    filesystem operation and intersect the two snapshots.  Keeping the final
    read and prune in one blocking call also removes an event-loop suspension
    where stale plaintext previously remained trivially reproducible.
    """

    live_at_end = storage.list_live_knowledge_ids(user_id)
    return vault.prune_orphans(user_id, live_at_start.intersection(live_at_end))


class WorkersManager:
    def __init__(
        self,
        settings: FridaySettings,
        storage,
        ingestion,
        kg,
        memory_vault=None,
        llm=None,
        executive=None,
        embeddings=None,
        extra_workers: Sequence[IntervalTask] = (),
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.ingestion = ingestion
        self.kg = kg
        self.memory_vault = memory_vault
        self.llm = llm
        self.executive = executive
        self.embeddings = embeddings
        # Монотонная отметка «не раньше»: см. _embeddings_index_all.
        self._embeddings_rest_until = 0.0
        self._embeddings_in_tick = False
        # Organ-contributed periodic tasks (JOP). Registered after the built-ins
        # so an organ can never shadow a core worker's name silently.
        self._extra_workers: tuple[IntervalTask, ...] = tuple(extra_workers)
        self.supervisor = WorkerSupervisor(self._persist_worker_state)
        # Last known failing/healthy state per worker, for transition detection.
        self._worker_failing: dict[str, bool] = {}
        self._registered = False
        self._run_task: asyncio.Task[Any] | None = None

    def register_all(self) -> None:
        if self._registered:
            return
        self._registered = True
        self.supervisor.register(
            "entity_resolution",
            self._entity_resolution_all,
            300,
            timeout_sec=240,
        )
        self.supervisor.register(
            "knowledge_quality_scan",
            self._knowledge_quality_scan_all,
            12 * 3600,
            run_immediately=False,
            timeout_sec=900,
        )
        # Прогрев экрана «Ревизия». Первый заход после перезапуска стоит ~17 с:
        # предикат читает содержимое всех документов и в SQL не выражается,
        # поэтому проход честно классифицирует весь архив. Вердикты помнятся по
        # ревизии объекта, так что достаточно сделать проход ОДИН раз — дальше
        # страница открывается за 0.2 с. В фоне это никому не мешает, а человек,
        # открывший раздел, не ждёт.
        self.supervisor.register(
            "revision_warmup",
            self._revision_warmup_all,
            24 * 3600,
            run_immediately=True,
            timeout_sec=600,
        )
        self.supervisor.register(
            "inbox_model_advice",
            self._inbox_model_advice_all,
            self.settings.cognition_interval_sec,
            enabled=bool(
                self.settings.cognition_enabled
                and self.llm is not None
                and getattr(self.llm, "enabled", False)
            ),
            run_immediately=False,
            timeout_sec=max(60.0, min(600.0, self.settings.llm_timeout_sec * 2.0)),
        )
        self.supervisor.register(
            "lifecycle_management",
            self._lifecycle_all,
            6 * 3600,
            timeout_sec=600,
        )
        # Disabled means there is no plaintext projector in the supervisor at
        # all.  Merely registering a dormant task makes an accidental injected
        # vault look like a rollout flag and leaves stale worker state behind.
        if self.settings.memory_vault_mode == "full_owner" and self.memory_vault is not None:
            self.supervisor.register(
                "memory_vault_sync",
                self._vault_sync_all,
                300,
                timeout_sec=900,
            )
        self.supervisor.register(
            "mission_runner",
            self._mission_runner_tick,
            self.settings.executive_tick_interval_sec,
            enabled=bool(self.settings.autonomy_enabled and self.executive is not None),
            run_immediately=False,
            timeout_sec=900,
        )
        self.supervisor.register(
            "mission_proposer",
            self._mission_proposer_all,
            self.settings.cognition_interval_sec,
            enabled=bool(
                self.settings.cognition_enabled
                and self.settings.autonomy_enabled
                and self.executive is not None
                and self.llm is not None
                and getattr(self.llm, "enabled", False)
            ),
            run_immediately=False,
            timeout_sec=max(60.0, min(600.0, self.settings.llm_timeout_sec * 2.0)),
        )
        self.supervisor.register(
            "embeddings_index",
            self._embeddings_index_all,
            self.settings.embeddings_index_interval_sec,
            enabled=bool(self.embeddings is not None and getattr(self.embeddings, "remote_enabled", False)),
            run_immediately=False,
            timeout_sec=max(60.0, min(600.0, self.settings.llm_timeout_sec * 2.0)),
        )
        self.supervisor.register(
            "entity_mention_backfill",
            self._entity_mention_backfill_all,
            # Раз в четверть часа: каждый вызов сам кооперативно ограничен временем,
            # восемью документами и 25 links, поэтому внешний timeout не используется
            # как штатный способ остановить неотменяемый Python thread.
            900.0,
            run_immediately=False,
            timeout_sec=300,
        )
        self.supervisor.register(
            "document_catalog_reconcile",
            self._document_catalog_reconcile_all,
            60.0,
            run_immediately=True,
            timeout_sec=120,
        )
        # Durable assist ownership outlives both a model endpoint and the rollout
        # flag that admitted it.  Keep retirement in the unconditional core worker
        # set so rollback cannot strand an active graph indefinitely.
        self.supervisor.register(
            "semantic_supervisor_graph_expiry",
            self._semantic_supervisor_graph_expiry,
            60.0,
            run_immediately=True,
            timeout_sec=30,
        )
        self.supervisor.register(
            "knowledge_dedup",
            self._knowledge_dedup_all,
            self.settings.dedup_interval_sec,
            enabled=bool(self.embeddings is not None and getattr(self.embeddings, "remote_enabled", False)),
            run_immediately=False,
            timeout_sec=900,
        )
        self.supervisor.register(
            "retrieval_eval",
            self._retrieval_eval_all,
            self.settings.eval_interval_sec,
            enabled=bool(self.settings.eval_enabled),
            run_immediately=False,
            timeout_sec=900,
        )
        self.supervisor.register(
            "approval_hygiene",
            self._approval_hygiene,
            # Раз в пять минут. Обе операции — по одному UPDATE с индексом, и обе
            # нужны РЕГУЛЯРНО, а не «когда-нибудь»: просроченное согласие, которое
            # ещё числится действующим, и оборванное исполнение, о котором никто не
            # знает, — это как раз то, что должно всплывать само.
            300.0,
            run_immediately=True,
            timeout_sec=60,
        )
        self.supervisor.register(
            "scheduled_backup",
            self._scheduled_backup,
            3600,
            timeout_sec=900,
        )
        self.supervisor.register(
            "database_optimize",
            self._database_optimize,
            3600,
            timeout_sec=300,
        )
        for task in self._extra_workers:
            self.supervisor.register(
                task.name,
                task.func,
                task.interval_sec,
                enabled=task.enabled,
                run_immediately=task.run_immediately,
                timeout_sec=task.timeout_sec,
            )

    # Statuses that mean the task did not do its job this round.
    _FAILED_STATUSES = frozenset({"error", "timeout"})

    def _persist_worker_state(self, name: str, state: dict[str, Any]) -> None:
        self.storage.kv_set(
            f"workers:health:{name}",
            json.dumps(state, ensure_ascii=False, sort_keys=True),
        )
        self._record_worker_transition(name, state)

    def _record_worker_transition(self, name: str, state: dict[str, Any]) -> None:
        """Journal the moment a worker starts failing, and the moment it recovers.

        Transitions rather than states, deliberately. ``kv_set`` above already holds
        the current health; what it cannot answer is "did anything break overnight and
        did it come back". Recording every tick would answer that too, but a task on a
        60-second interval broken for eight hours would write 480 rows to say one thing.
        """
        status = str(state.get("status") or "")
        if status not in {"ok", *self._FAILED_STATUSES}:
            return  # "running" is a heartbeat, not an event
        failing = status in self._FAILED_STATUSES
        previous = self._worker_failing.get(name)
        if previous == failing:
            return
        self._worker_failing[name] = failing
        if previous is None and not failing:
            return  # first successful run after start is not a recovery
        try:
            self.storage.record_event(
                "worker.failed" if failing else "worker.recovered",
                {
                    "worker": name,
                    "status": status,
                    # The message is already sanitised for logs by the supervisor.
                    "error_type": state.get("error_type"),
                    "consecutive_failures": state.get("consecutive_failures"),
                },
            )
        except Exception as exc:
            # Journalling must never take down the worker it is observing.
            LOGGER.debug("Could not record worker transition for %s (%s)", name, type(exc).__name__)

    async def _mission_runner_tick(self) -> None:
        # Advance a bounded number of ready mission tasks; the executive keeps
        # each tick short so this never overlaps or stalls the heartbeat.
        if self.executive is None:
            return
        await self.executive.tick()

    async def _mission_proposer_all(self) -> None:
        await self._for_each_user(self._mission_proposer)

    async def _mission_proposer(self, user_id: str) -> None:
        if self.executive is None:
            return
        await self.executive.maybe_propose_from_backlog(user_id)

    async def _tenants(self) -> list[str]:
        """Арендаторы, чей МАТЕРИАЛ обрабатывают фоновые работники.

        В общем архиве (`FRIDAY_SHARED_ARCHIVE`) материал у всех один, и список
        людей арендаторами быть перестал. Перебор по людям означал N−1 холостых
        проходов дедупа, разрешения сущностей, качества и жизненного цикла — и,
        что хуже, `_knowledge_dedup_all` делил на это N бюджет времени: работа
        есть у одного арендатора, а секунд ему доставалась 1/N.
        """
        from friday.permissions import LEGACY_OWNER_USER_ID

        if getattr(self.settings, "shared_archive", False):
            return [LEGACY_OWNER_USER_ID]
        return list(await run_blocking(self.storage.list_user_ids, active_only=True))

    async def _for_each_user(
        self,
        operation: Callable[[str], Awaitable[Any]],
        *,
        user_ids: Sequence[str] | None = None,
    ) -> None:
        failures = 0
        # Every storage call a worker makes from coroutine context goes through
        # `run_blocking`. A read does not take the write lock, but it still does
        # disk IO on the event loop that serves the API, the Telegram bridge and
        # every other worker — and a WRITE waits for the process-wide write lock,
        # measured at 3.00 s while one batch of embedding vectors was committing.
        # The rule is worth more than each individual saving: "no storage call on
        # the loop" is checkable, "this particular one is cheap" is not.
        tenants = list(user_ids) if user_ids is not None else await self._tenants()
        for user_id in tenants:
            try:
                await operation(user_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                LOGGER.error("Worker tenant operation failed (%s)", type(exc).__name__)
            await asyncio.sleep(0)
        if failures:
            raise WorkerBatchError(f"{failures} tenant operation(s) failed")

    async def _retrieval_eval_all(self) -> None:
        await self._for_each_user(self._retrieval_eval)

    async def _retrieval_eval(self, user_id: str) -> None:
        if not await run_blocking(self.storage.list_eval_cases, user_id):
            return
        from friday.eval import run_eval

        # Prune before measuring: a case whose expected objects were all deleted can
        # never be satisfied and would depress recall for good. Mined rows only —
        # a hand-curated case is never touched, however stale it looks.
        hygiene = await run_blocking(self.storage.prune_eval_cases, user_id)
        report = await run_eval(self.storage, self.embeddings, self.settings, user_id, k=self.settings.eval_k)
        if isinstance(report.get("gold_set"), dict):
            report["gold_set"]["pruned"] = hygiene
        await run_blocking(
            self.storage.kv_set, f"eval:last_report:{user_id}", json.dumps(report, ensure_ascii=False)
        )

    async def _entity_mention_backfill_all(self) -> None:
        # ``run_blocking`` uses a thread which an asyncio timeout cannot cancel.
        # Divide a conservative inner budget between tenants so the thread returns
        # cooperatively long before the supervisor's five-minute last-resort guard.
        tenants = max(1, len(await self._tenants()))
        share = max(1.0, min(10.0, 120.0 / tenants))
        await self._for_each_user(functools.partial(self._entity_mention_backfill, max_seconds=share))

    async def _entity_mention_backfill(self, user_id: str, *, max_seconds: float = 10.0) -> None:
        """Догнать старые документы уже существующими сущностями.

        Связи ставятся только при разборе, поэтому сущность, родившаяся поздно, к
        прежним документам не возвращается. Замерено на архиве владельца: 1173 пары
        «имя в тексте, связи нет» на 645 документах — это и есть главная причина, по
        которой граф не растёт.
        """
        report = await run_blocking(
            self.storage.backfill_entity_mentions,
            user_id,
            max_documents=8,
            max_seconds=max_seconds,
            max_links=25,
        )
        if report.get("linked"):
            LOGGER.info(
                "entity mentions backfilled: %d links over %d documents",
                int(report["linked"]),
                int(report.get("scanned") or 0),
            )

    async def _document_catalog_reconcile_all(self) -> None:
        """Converge the body-free document catalog within one global item budget."""

        # list_user_ids has a stable contract, but not a stable presentation order.
        # Sorting gives the numeric rotation a restart-safe meaning. Tenant phase
        # cursors live under one-way identifiers and their exact TEXT is base64
        # encoded, so this one compact runtime row contains no plaintext account id.
        tenant_ids = sorted(set(await run_blocking(self.storage.list_document_catalog_owner_ids)))
        tenant_count = len(tenant_ids)
        raw_state = await run_blocking(self.storage.kv_get, _DOCUMENT_CATALOG_CURSOR_KEY)
        state, state_supported = decode_document_catalog_worker_state(raw_state)
        if raw_state is not None and not state_supported:
            raise RuntimeError("document catalog worker state is unsupported")
        namespace_key = await run_blocking(
            load_document_catalog_worker_namespace_key,
            self.storage,
        )
        tenant_keys = {
            user_id: document_catalog_worker_tenant_key(
                user_id,
                namespace_key=namespace_key,
            )
            for user_id in tenant_ids
        }
        active_tenant_keys = set(tenant_keys.values())
        state.tenants = {
            tenant_key: phases
            for tenant_key, phases in state.tenants.items()
            if tenant_key in active_tenant_keys
        }
        if tenant_count == 0:
            empty = DocumentCatalogWorkerState()
            payload = encode_document_catalog_worker_state(empty)
            if raw_state is not None and raw_state != payload:
                stored = await run_blocking(
                    self.storage.checkpoint_document_catalog_worker_state,
                    expected_value=raw_state,
                    value=payload,
                    tenant_ids=tenant_ids,
                )
                if not stored:
                    raise RuntimeError("document catalog worker state changed during checkpoint")
            return

        cursor = state.cursor % tenant_count
        round_number = state.round
        base_share, remainder = divmod(_DOCUMENT_CATALOG_TICK_LIMIT, tenant_count)
        ordinary_count = tenant_count if base_share else remainder
        failed_tenants = [
            user_id
            for user_id in tenant_ids
            if (
                (tenant_state := state.tenants.get(tenant_keys[user_id])) is not None
                and (tenant_state.reconcile_failed or tenant_state.backfill_failed)
            )
        ]
        retry_user_id = (
            sorted(failed_tenants, key=tenant_keys.__getitem__)[state.retry % len(failed_tenants)]
            if failed_tenants
            else None
        )
        ordinary_user_ids = [tenant_ids[(cursor + offset) % tenant_count] for offset in range(ordinary_count)]
        if retry_user_id is not None and retry_user_id not in ordinary_user_ids:
            # Reserve one slot for sticky failure health, but advance the ordinary
            # rotation by only the slots actually visited. No healthy tenant is
            # skipped and a broken tenant cannot pin a fixed 128-tenant window.
            ordinary_count -= 1
            ordinary_user_ids = ordinary_user_ids[:ordinary_count]
            selected_tenants = [retry_user_id, *ordinary_user_ids]
        else:
            selected_tenants = ordinary_user_ids
        extra_positions = {(round_number + offset) % tenant_count for offset in range(remainder)}
        allocations: dict[str, tuple[int, int, str | None]] = {}
        for offset, user_id in enumerate(ordinary_user_ids):
            absolute_position = cursor + offset
            index = absolute_position % tenant_count
            visit_round = round_number + absolute_position // tenant_count
            share = 1 if base_share == 0 else base_share + int(index in extra_positions)
            tenant_key = tenant_keys[user_id]
            state.tenants.setdefault(tenant_key, DocumentCatalogTenantState())
            # An ordinary visit keeps the round-based phase alternation even
            # while its sibling phase is failing. Only the separately reserved
            # retry slot below is allowed to force a failed phase; otherwise a
            # permanent reconcile failure would starve healthy backfill forever.
            allocations[user_id] = (share, visit_round, None)
        if retry_user_id is not None and retry_user_id not in allocations:
            tenant_key = tenant_keys[retry_user_id]
            tenant_state = state.tenants[tenant_key]
            failed_phases = [
                phase for phase in ("reconcile", "backfill") if getattr(tenant_state, f"{phase}_failed")
            ]
            assert failed_phases
            allocations[retry_user_id] = (
                1,
                round_number,
                failed_phases[state.retry % len(failed_phases)],
            )
        rotation_position = cursor + ordinary_count
        rotation_checkpoint = (
            rotation_position % tenant_count,
            round_number + rotation_position // tenant_count,
        )

        totals = {
            "tenants": 0,
            "reconciled": 0,
            "backfilled": 0,
            "reconcile_has_more": 0,
            "backfill_has_more": 0,
            "phase_failures": 0,
        }
        reports_by_user: dict[str, dict[str, Any]] = {}

        def record_report(user_id: str, report: dict[str, Any]) -> None:
            tenant_state = state.tenants[tenant_keys[user_id]]
            for phase, phase_cursor in report["phase_updates"].items():
                setattr(tenant_state, phase, phase_cursor)
            for phase in report["successful_phases"]:
                setattr(tenant_state, f"{phase}_failed", False)
            for phase in report["failed_phases"]:
                setattr(tenant_state, f"{phase}_failed", True)
            for key in (
                "reconciled",
                "backfilled",
                "reconcile_has_more",
                "backfill_has_more",
                "phase_failures",
            ):
                totals[key] += report[key]

        async def converge(user_id: str) -> None:
            share, visit_round, forced_phase = allocations[user_id]
            tenant_key = tenant_keys[user_id]
            tenant_state = state.tenants[tenant_key]
            report = await self._document_catalog_reconcile(
                user_id,
                limit=share,
                phase_cursors={
                    "reconcile": tenant_state.reconcile,
                    "backfill": tenant_state.backfill,
                },
                backfill_only=bool(
                    forced_phase == "backfill" or (forced_phase is None and share == 1 and visit_round % 2)
                ),
            )
            reports_by_user[user_id] = report
            record_report(user_id, report)
            totals["tenants"] += 1
            if report["phase_failures"]:
                raise _DocumentCatalogPhaseError(
                    f"{report['phase_failures']} document catalog phase(s) failed"
                )

        try:
            batch_failure: WorkerBatchError | None = None
            try:
                await self._for_each_user(converge, user_ids=selected_tenants)
            except WorkerBatchError as exc:
                batch_failure = exc

            # Equal tenant reservations keep the first pass fair, but owners with
            # short corpora often return most of their share unused. Give that
            # *reported* remainder to phases which proved that they have a tail.
            # Backfill goes first so a large legacy owner does not advance only one
            # item per minute while healthy reconciliation repeatedly scans it.
            # A failed or missing report keeps its whole reservation: storage work
            # may have happened before the exception, so reclaiming it could exceed
            # the hard global item bound.
            spent = sum(
                reports_by_user[user_id]["budget_spent"]
                if user_id in reports_by_user
                else allocations[user_id][0]
                for user_id in selected_tenants
            )
            remaining = _DOCUMENT_CATALOG_TICK_LIMIT - spent
            redistribution_order = [
                tenant_ids[(cursor + round_number + offset) % tenant_count] for offset in range(tenant_count)
            ]
            selected = set(selected_tenants)
            for phase in ("backfill", "reconcile"):
                candidates = [
                    user_id
                    for user_id in redistribution_order
                    if user_id in selected
                    and user_id in reports_by_user
                    and reports_by_user[user_id][f"{phase}_has_more"]
                    and phase in reports_by_user[user_id]["successful_phases"]
                ]
                for user_id in candidates:
                    if remaining <= 0:
                        break
                    tenant_state = state.tenants[tenant_keys[user_id]]
                    report = await self._document_catalog_reconcile(
                        user_id,
                        limit=remaining,
                        phase_cursors={
                            "reconcile": tenant_state.reconcile,
                            "backfill": tenant_state.backfill,
                        },
                        backfill_only=phase == "backfill",
                        reconcile_only=phase == "reconcile",
                    )
                    record_report(user_id, report)
                    remaining -= report["budget_spent"]
                    if report["phase_failures"]:
                        break
                if remaining <= 0:
                    break

            unresolved_failures = sum(
                int(tenant_state.reconcile_failed) + int(tenant_state.backfill_failed)
                for tenant_state in state.tenants.values()
            )
            if unresolved_failures and batch_failure is None:
                batch_failure = WorkerBatchError(
                    f"{unresolved_failures} unresolved document catalog phase failure(s)"
                )

            # Rotation progresses independently of tenant health. Sticky phase
            # flags keep worker health failed and reserve a bounded retry slot.
            # Cancellation skips this one write: replay remains idempotent.
            state.cursor, state.round = rotation_checkpoint
            if failed_tenants:
                state.retry += 1
            payload = encode_document_catalog_worker_state(state)
            if raw_state != payload:
                stored = await run_blocking(
                    self.storage.checkpoint_document_catalog_worker_state,
                    expected_value=raw_state,
                    value=payload,
                    tenant_ids=tenant_ids,
                )
                if not stored:
                    raise RuntimeError("document catalog worker state changed during checkpoint")
            if batch_failure is not None:
                raise batch_failure
        finally:
            if any(totals[key] for key in totals if key != "tenants"):
                LOGGER.info(
                    "Document catalog convergence: tenants=%d reconciled=%d backfilled=%d "
                    "reconcile_has_more=%d backfill_has_more=%d phase_failures=%d",
                    totals["tenants"],
                    totals["reconciled"],
                    totals["backfilled"],
                    totals["reconcile_has_more"],
                    totals["backfill_has_more"],
                    totals["phase_failures"],
                )

    async def _document_catalog_reconcile(
        self,
        user_id: str,
        *,
        limit: int,
        phase_cursors: dict[str, str | None],
        backfill_only: bool = False,
        reconcile_only: bool = False,
    ) -> dict[str, Any]:
        """Spend one share in order, or use a fair single-item backfill slot."""

        if backfill_only and reconcile_only:
            raise ValueError("document catalog phase selection is ambiguous")
        phase_updates: dict[str, str | None] = {}
        successful_phases: set[str] = set()
        failed_phases: set[str] = set()
        if backfill_only:
            reserved_backfill: dict[str, Any] = {}
            failures = 0
            backfilled = 0
            backfill_has_more = 0
            try:
                reserved_backfill = await run_blocking(
                    self.storage.backfill_document_catalog,
                    user_id,
                    after_raw_object_id=phase_cursors["backfill"],
                    limit=limit,
                )
                _spent, has_more, next_cursor = _document_catalog_phase_page(
                    reserved_backfill,
                    spent_key="examined",
                    limit=limit,
                )
                backfilled = _document_catalog_processed(reserved_backfill, maximum=_spent)
                phase_updates["backfill"] = next_cursor
                backfill_has_more = int(has_more)
                successful_phases.add("backfill")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures = 1
                failed_phases.add("backfill")
                _spent = limit
                LOGGER.error("Document catalog backfill phase failed (%s)", type(exc).__name__)
            return {
                "reconciled": 0,
                "backfilled": backfilled,
                "reconcile_has_more": 0,
                "backfill_has_more": backfill_has_more,
                "phase_failures": failures,
                "phase_updates": phase_updates,
                "successful_phases": successful_phases,
                "failed_phases": failed_phases,
                "budget_spent": _spent,
            }

        if reconcile_only:
            failures = 0
            reconciled = 0
            reconcile_has_more = 0
            try:
                reconcile_page = await run_blocking(
                    self.storage.reconcile_document_catalog,
                    user_id,
                    after_raw_object_id=phase_cursors["reconcile"],
                    limit=limit,
                )
                reconciled, has_more, next_cursor = _document_catalog_phase_page(
                    reconcile_page,
                    spent_key="examined",
                    limit=limit,
                )
                phase_updates["reconcile"] = next_cursor
                reconcile_has_more = int(has_more)
                successful_phases.add("reconcile")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures = 1
                failed_phases.add("reconcile")
                reconciled = limit
                LOGGER.error("Document catalog reconcile phase failed (%s)", type(exc).__name__)
            return {
                "reconciled": reconciled if not failures else 0,
                "backfilled": 0,
                "reconcile_has_more": reconcile_has_more,
                "backfill_has_more": 0,
                "phase_failures": failures,
                "phase_updates": phase_updates,
                "successful_phases": successful_phases,
                "failed_phases": failed_phases,
                "budget_spent": reconciled,
            }

        # Keep one item for backfill whenever the tenant owns at least two. A
        # permanently busy reconciliation queue must not starve legacy rows.
        reconcile_limit = limit - 1 if limit >= 2 else limit
        reconcile: dict[str, Any] = {}
        failures = 0
        reconcile_has_more = 0
        budget_spent = 0
        try:
            reconcile = await run_blocking(
                self.storage.reconcile_document_catalog,
                user_id,
                after_raw_object_id=phase_cursors["reconcile"],
                limit=reconcile_limit,
            )
            reconciled, has_more, next_cursor = _document_catalog_phase_page(
                reconcile,
                spent_key="examined",
                limit=reconcile_limit,
            )
            phase_updates["reconcile"] = next_cursor
            reconcile_has_more = int(has_more)
            successful_phases.add("reconcile")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            failed_phases.add("reconcile")
            LOGGER.error("Document catalog reconcile phase failed (%s)", type(exc).__name__)
            # The reserved item is safe even if an implementation did work before
            # raising. Spending the whole apparent remainder would not be bounded.
            backfill_limit = 1 if limit >= 2 else 0
            reconciled = 0
            budget_spent = reconcile_limit
        else:
            backfill_limit = limit - reconciled
            budget_spent = reconciled
        backfill: dict[str, Any] = {}
        backfilled = 0
        backfill_has_more = 0
        if backfill_limit > 0:
            try:
                backfill = await run_blocking(
                    self.storage.backfill_document_catalog,
                    user_id,
                    after_raw_object_id=phase_cursors["backfill"],
                    limit=backfill_limit,
                )
                backfill_spent, has_more, next_cursor = _document_catalog_phase_page(
                    backfill,
                    spent_key="examined",
                    limit=backfill_limit,
                )
                backfilled = _document_catalog_processed(backfill, maximum=backfill_spent)
                phase_updates["backfill"] = next_cursor
                backfill_has_more = int(has_more)
                successful_phases.add("backfill")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                failed_phases.add("backfill")
                budget_spent += backfill_limit
                LOGGER.error("Document catalog backfill phase failed (%s)", type(exc).__name__)
            else:
                budget_spent += backfill_spent
        return {
            "reconciled": reconciled,
            "backfilled": backfilled,
            "reconcile_has_more": reconcile_has_more,
            "backfill_has_more": backfill_has_more,
            "phase_failures": failures,
            "phase_updates": phase_updates,
            "successful_phases": successful_phases,
            "failed_phases": failed_phases,
            "budget_spent": budget_spent,
        }

    async def _knowledge_dedup_all(self) -> None:
        # The tick budget is split across tenants, so no single scan can run the whole
        # tick past the supervisor's timeout — asyncio.to_thread is not interruptible,
        # and an over-long scan would show up as a recurring `timeout` in worker health
        # while quietly still doing the work.
        tenants = max(1, len(await self._tenants()))
        share = max(1.0, float(self.settings.dedup_scan_max_seconds) / tenants)
        await self._for_each_user(functools.partial(self._knowledge_dedup, max_seconds=share))

    async def _knowledge_dedup(self, user_id: str, *, max_seconds: float | None = None) -> None:
        from friday.dedup import detect_near_duplicates

        result = await run_blocking(
            functools.partial(detect_near_duplicates, max_seconds=max_seconds),
            self.storage,
            self.settings,
            user_id,
        )
        if result.get("detected"):
            LOGGER.info("Near-duplicate scan: %d proposal(s)", result["detected"])
        if result.get("incomplete"):
            LOGGER.info(
                "Near-duplicate scan paused with %d object(s) pending; resumes next tick",
                int(result.get("pending") or 0),
            )

    async def _entity_resolution_all(self) -> None:
        await self._for_each_user(self._entity_resolution)

    async def _entity_resolution(self, user_id: str) -> None:
        """Один бюджетный тик обхода, а не полный проход.

        Полный проход по 2000 плотных сущностей замерен в 137 с при таймауте
        воркера 240 с — то есть до отказа оставался один порядок роста графа.
        Тик доходит до конца за несколько заходов и говорит, сколько осталось.
        """
        report = await run_blocking(self.kg.resolver.sweep_duplicates, user_id, min_confidence=0.60)
        if report.get("suggested"):
            LOGGER.info("Suggested %d potential entity merges", report["suggested"])
        if report.get("partial"):
            LOGGER.info(
                "Entity duplicate sweep is %d/%d keys in; continuing next tick",
                report.get("keys_examined", 0),
                report.get("keys_total", 0),
            )

    async def _revision_warmup_all(self) -> None:
        await self._for_each_user(self._revision_warmup)

    async def _revision_warmup(self, user_id: str) -> None:
        """Один проход ревизии, только чтение — чтобы страница открывалась сразу."""
        await run_blocking(self.ingestion.scan_legacy_quality_page, user_id, limit=25, offset=0)

    async def _knowledge_quality_scan_all(self) -> None:
        await self._for_each_user(self._knowledge_quality_scan)

    async def _knowledge_quality_scan(self, user_id: str) -> None:
        """Refresh a read-only quality report; never mutate or hide knowledge automatically."""

        candidates = await run_blocking(
            self.ingestion.scan_legacy_quality,
            user_id,
            limit=250,
        )
        summary = {
            "scanned_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "candidate_count": len(candidates),
            "top_candidates": [
                {
                    "knowledge_object_id": item["knowledge_object"]["id"],
                    "title": item["knowledge_object"].get("title", ""),
                    "risk_score": item["risk_score"],
                    "recommended_action": item["recommended_action"],
                    "reasons": item["reasons"],
                }
                for item in candidates[:25]
            ],
        }
        await run_blocking(
            self.storage.kv_set,
            f"workers:knowledge_quality:{user_id}",
            json.dumps(summary, ensure_ascii=False, sort_keys=True),
        )
        if candidates:
            LOGGER.info(
                "Quality review found %d conservative candidates",
                len(candidates),
            )

    async def _inbox_model_advice_all(self) -> None:
        await self._for_each_user(self._inbox_model_advice)

    def _remember_advice_failure(self, user_id: str, item: dict[str, Any]) -> None:
        """Отметить на объекте, что совет по нему не получился.

        Пишется через `update_inbox_suggestions`, а не через смену статуса: это
        машинная отметка, и она не должна выглядеть решением человека — граница,
        которую сам метод и охраняет.
        """
        inbox_id = str(item.get("id") or "")
        if not inbox_id:
            return
        raw = item.get("suggestions_json")
        if isinstance(raw, str):
            try:
                suggestions = json.loads(raw or "{}")
            except (TypeError, ValueError):
                suggestions = {}
        else:
            suggestions = dict(raw or {})
        if not isinstance(suggestions, dict):
            suggestions = {}
        suggestions["model_advice_failures"] = _advice_attempts(item) + 1
        with suppress(Exception):
            # Отметка вспомогательная: если записать её не вышло, это не повод
            # заваливать тик — в худшем случае объект будет опробован ещё раз.
            self.storage.update_inbox_suggestions(inbox_id, user_id, suggestions=suggestions)

    async def _inbox_model_advice(self, user_id: str) -> None:
        """Add bounded local-model advice to a few pending items per cycle.

        The ingestion boundary guarantees that this task cannot classify,
        promote, create entities, or merge anything.  Small batches keep
        foreground Telegram requests ahead of background cognition.
        """

        if self.llm is None or not getattr(self.llm, "enabled", False):
            return
        processed = 0
        failures = 0
        consecutive_failures = 0
        pending = await run_blocking(
            self.storage.list_inbox_detailed,
            user_id,
            InboxStatus.PENDING,
            limit=50,
        )
        for item in pending:
            if _advice_attempts(item) >= _ADVICE_ITEM_ATTEMPTS:
                # Объект, на котором совет не выходит раз за разом, пропускается —
                # см. `_ADVICE_ITEM_ATTEMPTS`. Он остаётся в очереди человека, просто
                # без машинного совета.
                continue
            try:
                result = await self.ingestion.advise_inbox_item(
                    user_id,
                    item["id"],
                    llm=self.llm,
                    requested_by="worker:inbox_model_advice",
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures += 1
                if _is_endpoint_failure(error):
                    consecutive_failures += 1
                    LOGGER.warning(
                        "Inbox model advice: model endpoint failed (%s)",
                        type(error).__name__,
                    )
                else:
                    # Беда объекта, а не канала: счётчик недоступности не трогаем, а
                    # неудачу запоминаем на самом объекте.
                    consecutive_failures = 0
                    LOGGER.warning(
                        "Inbox model advice: item could not be advised (%s)",
                        type(error).__name__,
                    )
                    await run_blocking(self._remember_advice_failure, user_id, item)
                if consecutive_failures >= _ADVICE_ENDPOINT_DOWN_AFTER:
                    # Isolated failures are per-item and worth stepping over; this many
                    # in a row is the endpoint, not the documents. Continuing would run
                    # the remaining items through three LLM retries each and exhaust the
                    # worker's whole budget for nothing — observed here as 46 failures
                    # and an eight-minute timeout while the endpoint was down. Stopping
                    # also stops hammering something that is already struggling.
                    LOGGER.warning(
                        "Inbox model advice stopped after %d consecutive failures; "
                        "treating the model endpoint as unavailable",
                        consecutive_failures,
                    )
                    break
                continue
            consecutive_failures = 0
            if result.get("idempotent_replay"):
                continue
            processed += 1
            if processed >= 2:
                break
        if processed:
            LOGGER.info("Added local-model advice to %d Inbox items", processed)
        if failures:
            raise WorkerBatchError(f"{failures} Inbox advice item(s) failed")

    async def _lifecycle_all(self) -> None:
        await self._for_each_user(self._lifecycle_management)

    async def _lifecycle_management(self, user_id: str) -> None:
        candidates = await run_blocking(
            self.storage.list_lifecycle_candidates,
            user_id,
            days_threshold=90,
            limit=250,
        )
        report = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "candidate_count": len(candidates),
            "candidates": [
                {
                    "knowledge_object_id": item["knowledge_object"]["id"],
                    "title": item["knowledge_object"].get("title", ""),
                    "risk_score": item["risk_score"],
                    "recommended_action": item["recommended_action"],
                    "suggested_importance": item["suggested_importance"],
                    "reasons": item["reasons"],
                }
                for item in candidates[:100]
            ],
        }
        await run_blocking(
            self.storage.kv_set,
            f"workers:lifecycle:{user_id}",
            json.dumps(report, ensure_ascii=False, sort_keys=True),
        )
        if candidates:
            LOGGER.info(
                "Lifecycle review proposed %d candidates; no changes applied",
                len(candidates),
            )

    async def _vault_sync_all(self) -> None:
        await self._for_each_user(self._vault_sync)

    async def _vault_sync(self, user_id: str) -> None:
        if self.memory_vault is None:
            return
        offset = 0
        vault = self.memory_vault
        # The prune set is ONE snapshot, taken before the render loop — not the sum
        # of the pages that loop reads. Paging orders by `importance DESC,
        # updated_at DESC` and both keys change under concurrent edits, so an
        # object edited while the loop runs can move across a page boundary and
        # appear in NEITHER page. `prune_orphans` would then see a live object as
        # an orphan and delete its note from the user's vault. Editing an object is
        # the commonest write there is and this loop runs every five minutes, so
        # the two overlap by construction.
        #
        # This is only the first half of the retention decision.  A final snapshot
        # below removes ids deleted/quarantined during rendering.  Their
        # intersection is deliberately conservative for new objects: a note first
        # seen mid-sweep may be pruned and rendered again on the next cycle, but a
        # stale private note is never kept merely because it was public at start.
        live_at_start: set[str] = await run_blocking(
            self.storage.list_live_knowledge_ids,
            user_id,
        )
        while True:
            objects = await run_blocking(
                self.storage.list_knowledge_objects, user_id, limit=_VAULT_PAGE, offset=offset
            )
            if not objects:
                break
            # One page per offload, not one file. `sync_object` is mkstemp + write +
            # **fsync** + os.replace for every object, and the loop had no `await` in
            # it at all, so a whole-corpus re-render ran on the event loop every 300
            # seconds — the fsyncs alone stall it for as long as the disk takes.
            await run_blocking(_sync_vault_page, vault, self.storage, objects)
            offset += len(objects)
            if len(objects) < _VAULT_PAGE:
                break
        # Only after the render loop has run to completion. An exception inside it
        # propagates and skips the prune entirely — a stale note beats a deleted one.
        #
        # Without it the vault only ever grew. `list_knowledge_objects` filters
        # `deleted_at IS NULL` and `delete_object` was called from the hard-purge path
        # alone, so a soft-deleted or IGNORED object kept a plaintext copy of its full
        # content on disk forever — while the user was told it was deleted and search
        # agreed with them.
        removed = await run_blocking(
            _prune_vault_against_final_snapshot,
            self.memory_vault,
            self.storage,
            user_id,
            live_at_start,
        )
        if removed:
            LOGGER.info("Vault sync removed %d note(s) for objects that are no longer live", removed)

    async def _scheduled_backup(self) -> None:
        raw = await run_blocking(self.storage.kv_get, "workers:last_backup_at")
        now = datetime.now(UTC)
        try:
            previous = datetime.fromisoformat(raw) if raw else None
            if previous and previous.tzinfo is None:
                previous = previous.replace(tzinfo=UTC)
        except ValueError:
            previous = None
        if previous and now - previous < timedelta(hours=24):
            return
        result = await run_blocking(self.storage.create_backup, label="scheduled")
        await run_blocking(self.storage.kv_set, "workers:last_backup_at", now.isoformat(timespec="seconds"))
        await run_blocking(self.storage.kv_set, "workers:last_backup", json.dumps(result, ensure_ascii=False))
        LOGGER.info("Scheduled database backup created")
        # Every backup, not just failures: "when did this last actually run" is the
        # question asked after something has already gone wrong, and by then the log
        # that would have answered it has usually rotated.
        await run_blocking(
            self.storage.record_event,
            "backup.created",
            {
                "database": result.get("database"),
                "size_bytes": result.get("size_bytes"),
                "integrity_check": result.get("integrity_check"),
                "label": "scheduled",
            },
        )
        # Оригиналы файлов: база уносит извлечённый текст, а PDF/сканы/фото/голос
        # существовали в одном экземпляре на том же диске. Инкремент дёшев: файлы
        # content-addressed и неизменяемы. Удаления НЕ распространяются.
        from friday.backup_files import backup_files_incremental

        files_result = await run_blocking(
            backup_files_incremental,
            self.settings.files_dir,
            self.settings.backups_dir / "files",
        )
        files_result["reported_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        await run_blocking(
            self.storage.kv_set,
            "workers:last_files_backup",
            json.dumps(files_result, ensure_ascii=False),
        )
        if files_result.get("copied") or files_result.get("failed"):
            await run_blocking(self.storage.record_event, "backup.files", files_result)
        # A same-disk backup is not a real backup: mirror it offsite when
        # configured (encrypted when a key file is set).
        from friday.backup_mirror import mirror_backups

        mirror = await run_blocking(mirror_backups, self.settings)
        if mirror.get("enabled"):
            # Stamped so diagnostics can tell "mirrored a moment ago" from
            # "mirrored last month and the disk has been unplugged since".
            mirror["reported_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            await run_blocking(
                self.storage.kv_set,
                "workers:last_backup_mirror",
                json.dumps(mirror, ensure_ascii=False),
            )
            # Дерево оригиналов едет в то же зеркало: mirror_backups ходит по
            # манифестам БД и файлы не видел — offsite-копии документов не было
            # даже у включившего зеркало.
            from friday.backup_mirror import mirror_files_tree

            files_mirror = await run_blocking(mirror_files_tree, self.settings)
            if files_mirror.get("enabled"):
                files_mirror["reported_at"] = datetime.now(UTC).isoformat(timespec="seconds")
                await run_blocking(
                    self.storage.kv_set,
                    "workers:last_files_mirror",
                    json.dumps(files_mirror, ensure_ascii=False),
                )
        # Prune AFTER mirroring, so an offsite copy of the oldest generation is made
        # before the local one goes. Retention is the missing half of a daily backup:
        # without it the disk fills, and it takes the live instance with it.
        pruned = await run_blocking(self.storage.prune_backups, keep=self.settings.backup_keep)
        if pruned.get("removed"):
            await run_blocking(self.storage.record_event, "backup.pruned", pruned)

    async def _embeddings_index_all(self) -> None:
        """Досчитывать, пока есть работа и не вышел бюджет ВРЕМЕНИ.

        Бюджет в символах на проход остаётся — он бережёт один запрос от таймаута, —
        но потолком тика он быть не может: 200 000 символов раз в две минуты это
        1 700 символов в секунду, а сервис на видеокарте делает 211 000. Число,
        выбранное под сервис на процессоре (768 симв/с), стало тормозом в сто раз:
        корпус в 25 млн символов закрывался бы четыре часа вместо минут.

        Время подстраивается само под любую скорость: медленный сервис успеет один
        проход, быстрый — сотню. Отдых после тика по-прежнему считается по времени,
        проведённому В сервисе, так что обратное давление не потеряно.
        """
        deadline = time.monotonic() + max(5.0, float(self.settings.embeddings_index_tick_budget_sec))
        try:
            await self._embeddings_drain(deadline)
        finally:
            self._embeddings_in_tick = False

    async def _embeddings_drain(self, deadline: float) -> None:
        while True:
            indexed = await self._embeddings_index_pass()
            self._embeddings_in_tick = True
            if not indexed or time.monotonic() >= deadline:
                return
            if getattr(self.embeddings, "cooling_down", False):
                return
            # Отдых ВЫДЕРЖИВАЕТСЯ здесь, а не обрывает тик. Проход назначает паузу
            # пропорционально времени в сервисе, и проверка этой паузы на входе в
            # следующий проход выходила боком: любой проход длиннее секунды ставил
            # отдых, следующий проход возвращал ноль, и цикл заканчивался. Бюджет
            # времени в 60 секунд тратился на ОДИН проход из восьми объектов —
            # корпус закрывался бы четыре часа вместо минут.
            rest = self._embeddings_rest_until - time.monotonic()
            if rest > 0:
                if time.monotonic() + rest >= deadline:
                    return
                await asyncio.sleep(rest)

    async def _embeddings_index_pass(self) -> int:
        """Embed a bounded batch of Knowledge Objects lacking a current vector.

        The batch spans all tenants: vectors depend only on object content, so the
        index reconciles the whole corpus and newly promoted or re-enriched objects
        become dense-searchable within one cycle. A backend outage simply defers the
        batch to the next tick — nothing is dropped.
        """
        if self.embeddings is None or not getattr(self.embeddings, "remote_enabled", False):
            return 0
        # Отдых пропорционально сделанной работе. Планировщик считает паузу как
        # `interval - elapsed`, что для дешёвых задач правильно, а здесь
        # вырождается: тик на 11 минут при интервале 2 минуты спит РОВНО СЕКУНДУ и
        # уходит на второй круг. Получается непрерывная нагрузка на чужой сервис,
        # и ровно она положила прокси перед эмбеддингами через полтора часа.
        now = time.monotonic()
        if now < self._embeddings_rest_until and not self._embeddings_in_tick:
            # Гейт стоит только на ВХОДЕ в тик: внутри тика паузу выдерживает цикл,
            # а не обрывает её проверкой.
            return 0
        if getattr(self.embeddings, "cooling_down", False):
            LOGGER.info(
                "embeddings index skipped: backend is cooling down for %.0fs",
                self.embeddings.cooldown_remaining(),
            )
            return 0
        model = self.settings.embeddings_model
        batch = self.settings.embeddings_index_batch
        scheme = chunk_scheme(self.settings)
        rows = await run_blocking(
            self.storage.list_knowledge_missing_embedding,
            model,
            limit=batch,
            chunk_scheme=scheme,
            chunk_threshold=self.settings.embeddings_chunk_chars,
        )
        if not rows:
            return 0

        cap = max(1, int(self.settings.embeddings_max_inputs_per_request))
        # An object is never split across requests, so its OWN input count must fit in
        # one: the whole-object text plus at most cap-1 passages. Without this clamp
        # the shipped defaults (64 chunks + 1 doc vs. a 64-input cap) overflow by one
        # on every maximally-split object, and an endpoint that enforces its batch
        # limit then rejects the request forever — leaving that object with no vector
        # at all, not even the whole-object one it had before chunking existed.
        max_chunks = max(1, min(int(self.settings.embeddings_chunk_max_per_object), cap - 1))

        # One work item per object: the whole-object text first, then its passages.
        # Keeping an object's inputs together means a failed request loses that object
        # and not the whole batch.
        plans: list[dict[str, Any]] = []
        char_budget = max(1000, int(self.settings.embeddings_index_char_budget))
        tick_chars = 0
        for row in rows:
            # Сколько пассажей дать ЭТОМУ объекту. Потолок из настроек — не про длину
            # текста, а про число входов; при нехватке пассажей `chunk_spans` не режет
            # документ, а РАСШИРЯЕТ окно, чтобы покрыть его целиком. Для очень длинных
            # документов окно вырастает выше того, что сервис принимает одним входом,
            # и дальше происходит тихая беда: запрос отвергают, длину режут вдвое, и
            # половина пассажа в свой вектор не попадает.
            #
            # Замерено на корпусе владельца: 569 пассажей из 13 840 (4.1%) шире
            # 10 000 знаков, все — у ДЕВЯТИ объектов из 1086, самый широкий 22 012.
            # То есть беда узкая, но бьёт по самым содержательным документам, и
            # ровно по тем, ради которых разбиение на пассажи и делалось.
            #
            # Лечение — дать столько пассажей, чтобы окно осталось в безопасных
            # границах, а не сужать покрытие. Верхняя граница прежняя: входы одного
            # объекта обязаны поместиться в один запрос.
            # Считать надо по тому, что реально уедет ОДНИМ входом, а не по ширине
            # окна: к куску тела приписывается шапка объекта, окна перекрываются, а
            # хвостовой огрызок подклеивается к предыдущему. Три надбавки, каждая
            # маленькая, и вместе они переносят вход через предел — первая версия
            # этого расчёта промахнулась ровно на них, на 111 знаков.
            overhead = (
                self.settings.embeddings_chunk_overlap_chars
                + max(120, self.settings.embeddings_chunk_chars // 6)
                + self.settings.embeddings_chunk_chars // 4
            )
            target = max(1, _DOC_VECTOR_MAX_CHARS - overhead)
            needed = math.ceil(len(str(row.get("content") or "")) / target)
            object_chunks = max(1, min(max(max_chunks, needed), cap - 1))
            units = knowledge_chunk_units(
                row,
                max_chars=self.settings.embeddings_chunk_chars,
                overlap_chars=self.settings.embeddings_chunk_overlap_chars,
                max_chunks=object_chunks,
            )
            # Bounded before it is sent. The whole-object vector is a COARSE signal —
            # passage vectors carry the detail — but it goes as one input, and a large
            # object makes that input alone exceed what an embeddings service will take.
            # Measured: 104175 characters timed out where 20000 answered in seconds.
            # Worse, the object's inputs travel in one request, so an oversized document
            # lost its passages too and ended up with NO vector at all — appearing in
            # the log as "backend returned no usable vectors" rather than as too long.
            doc_text = knowledge_search_text(row)[: min(_DOC_VECTOR_MAX_CHARS, _EMBED_INPUT_MAX_CHARS)]
            plans.append({"row": row, "doc_text": doc_text, "units": units})
            # Бюджет тика — в СИМВОЛАХ, а не в объектах. «64 объекта» ничего не
            # ограничивает: заметка и стостраничный docx отличаются в триста раз, и
            # на настоящем корпусе владельца та же пачка из 64 штук весит ~1.9 млн
            # символов, то есть примерно 11 минут работы при таймауте задачи в 600 с.
            # Тик убивался на середине, и вся сделанная в нём работа выбрасывалась.
            tick_chars += len(doc_text) + sum(len(unit[2]) for unit in units)
            if tick_chars >= char_budget:
                break

        for plan in plans:
            texts = [plan["doc_text"], *(unit[2] for unit in plan["units"])]
            plan["texts"] = texts
            plan["hashes"] = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts]

        reusable = await run_blocking(
            self.storage.get_reusable_vectors, [str(plan["row"]["id"]) for plan in plans], model
        )
        # Second lookup, by TEXT rather than by object: a vector for this exact text
        # may already exist under a different object. Costs one indexed query and
        # saves the model an entire re-embedding when a folder is imported twice.
        #
        # Но НЕ для объектов, помеченных на принудительный пересчёт. Поиск по хэшу
        # идёт по всей таблице, без арендатора, — в этом его польза при повторном
        # импорте и в этом же беда: `reindex-embeddings --user X` стирает хэши только
        # у X, а тот же текст у другого арендатора остаётся с прежним, возможно
        # негодным, вектором. Индексатор находил его, HTTP-вызова не делал и
        # переписывал строку как свежую. Пересчёт отчитывался об успехе, ничего не
        # пересчитав, — и вектор после этого выглядел честным.
        #
        # Признак принудительной пометки — пустой `content_hash` у собственной строки
        # объекта; после первой же успешной записи он исчезает сам.
        forced = {str(plan["row"]["id"]) for plan in plans if plan["row"].get("forced")}
        shared = await run_blocking(
            self.storage.get_vectors_by_content_hash,
            [digest for plan in plans if str(plan["row"]["id"]) not in forced for digest in plan["hashes"]],
            model,
        )
        groups: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_size = 0
        for plan in plans:
            texts = plan["texts"]
            hashes = plan["hashes"]
            # Помеченному на принудительный пересчёт кэш по хэшу не предлагается ВООБЩЕ.
            # Убрать его дайджесты из ЗАПРОСА недостаточно, и это была моя ошибка:
            # `shared` возвращается плоским словарём «дайджест -> вектор», и достаточно
            # ОДНОГО непомеченного соседа по пачке с тем же текстом, чтобы тот же
            # дайджест туда попал — а дальше помеченный объект находит его здесь и
            # уходит без обращения к сервису. Пересчёт снова отчитывается об успехе,
            # ничего не пересчитав. Найдено состязательным ревью, двумя срезами
            # независимо. `reusable` для помеченного и так пуст (его собственный хэш
            # стёрт), но полагаться на это нельзя — условие должно быть явным.
            forced_plan = str(plan["row"]["id"]) in forced
            known = {} if forced_plan else dict(shared)
            known.update(reusable.get(str(plan["row"]["id"]), {}))
            # Same text, same model -> the stored vector is still exact; skip the call.
            plan["cached"] = {index: known[digest] for index, digest in enumerate(hashes) if digest in known}
            plan["missing"] = [index for index in range(len(texts)) if index not in plan["cached"]]
            if current and current_size + len(plan["missing"]) > cap:
                groups.append(current)
                current, current_size = [], 0
            current.append(plan)
            current_size += len(plan["missing"])
        if current:
            groups.append(current)

        indexed = 0
        # Считается время СЕТЕВОЙ работы, а не всего тика. Разбор на чанки, выборка
        # из базы и попадания в кэш переиспользования занимают время, но чужой сервис
        # ими не нагружается, и отдыхать из-за медленной базы не от чего. Побочно это
        # убирает недетерминированность: с подставным бэкендом, отвечающим мгновенно,
        # сетевое время равно нулю и отдых не назначается вовсе.
        service_started = time.monotonic()
        for group in groups:
            if not await self._embed_group(group, model, scheme):
                continue
            indexed += len(group)
        elapsed = max(0.0, time.monotonic() - service_started)

        # Отдых назначается ПОСЛЕ работы и по её фактической длительности, а не по
        # плановой: скорость чужого сервиса нам неизвестна и меняется. Коэффициент 1.0
        # означает «работать не больше половины времени», и он же — единственная
        # величина, которой оператор может обменять скорость индексации на здоровье
        # сервиса, ничего не зная про символы в секунду.
        # Порог обязателен, и он не про тесты. Отдых существует, чтобы не насыщать
        # чужой сервис; тик, уложившийся в миллисекунды, ничего не насытил — там
        # либо нечего было делать, либо вектора пришли из кэша переиспользования.
        # Без порога любая пауза, пусть и микроскопическая, превращает «догнать
        # отставание за несколько тиков подряд» в невозможное.
        rest = 0.0
        if elapsed >= _EMBED_REST_MIN_WORK_SEC:
            rest = elapsed * max(0.0, float(self.settings.embeddings_index_rest_ratio))
            self._embeddings_rest_until = time.monotonic() + rest

        if indexed:
            remaining = await run_blocking(
                self.storage.count_knowledge_missing_embedding,
                model,
                chunk_scheme=scheme,
                chunk_threshold=self.settings.embeddings_chunk_chars,
            )
            # Прогресс говорится вслух: на корпусе в тысячи документов индексация
            # идёт часами, и «сколько ещё» — единственный способ отличить «работает»
            # от «встало». Раньше в логе была только строка про обновлённые объекты.
            LOGGER.info(
                "embeddings index updated %d objects in %.1fs, %d left, resting %.0fs",
                indexed,
                elapsed,
                remaining,
                rest,
            )
        return indexed

    async def _embed_in_volume_slices(self, texts: list[str]) -> list[list[float]] | None:
        """Embed in slices small enough to answer inside the request timeout.

        Order is preserved and the result is concatenated, so the caller's offset ledger
        still lines up. All-or-nothing on purpose: a partially embedded object would
        keep some passages and silently lose others, and nothing downstream could tell
        that apart from an object that simply has fewer passages.

        A single text larger than the budget still goes on its own — refusing it would
        drop content, and ``_DOC_VECTOR_MAX_CHARS`` already keeps the largest input far
        below what a service will take.
        """
        collected: list[list[float]] = []
        slice_texts: list[str] = []
        slice_chars = 0
        for text in [*texts, None]:
            over_budget = (
                text is not None and slice_texts and slice_chars + len(text) > _EMBED_REQUEST_MAX_CHARS
            )
            if (text is None or over_budget) and slice_texts:
                returned = await self.embeddings.embed(slice_texts)  # type: ignore[union-attr]
                if not returned or len(returned) != len(slice_texts):
                    return None
                collected.extend(returned)
                slice_texts, slice_chars = [], 0
            if text is not None:
                slice_texts.append(text)
                slice_chars += len(text)
        return collected

    async def _embed_group(self, group: list[dict[str, Any]], model: str, scheme: str) -> bool:
        """Embed one request-sized group and persist it; return False if it was lost.

        Written as an explicit offset/count ledger rather than ``zip``: with chunking a
        length mismatch would silently attribute plausible-looking vectors to the WRONG
        Knowledge Objects, which no test and no log line would ever reveal.
        """
        flattened: list[str] = []
        offsets: list[tuple[dict[str, Any], int, int]] = []
        for plan in group:
            start = len(flattened)
            flattened.extend(plan["texts"][index] for index in plan["missing"])
            offsets.append((plan, start, len(plan["missing"])))
        vectors: list[list[float]] = []
        if flattened:
            returned = await self._embed_in_volume_slices(flattened)
            if returned is None or len(returned) != len(flattened):
                LOGGER.warning(
                    "embeddings index skipped %d objects: backend returned no usable vectors",
                    len(group),
                )
                return False
            vectors = returned

        # Пометка могла прийти ПОКА пачка была в сервисе. План построен до неё, значит
        # `forced` у него нулевой и часть входов разрешена из кэша; записать такой план
        # означало бы стереть пометку вектором, который никто не пересчитывал. Ровно это
        # и происходит, если запустить `reindex-embeddings` при работающем backend, —
        # а команда именно на это и рассчитана. Проверка стоит здесь, а не блокировкой
        # процесса: заставлять останавливать сервис ради пересчёта значит потерять то,
        # ради чего команда сделана. Объект, помеченный на лету, просто пропускается и
        # берётся следующим тиком — уже без кэша.
        marked_now: set[str] = set()
        if any(plan["cached"] for plan, _, _ in offsets):
            marked_now = set(
                await run_blocking(
                    self.storage.list_forced_embedding_ids,
                    [str(plan["row"]["id"]) for plan, _, _ in offsets],
                )
            )

        items: list[dict[str, Any]] = []
        chunks: dict[str, Sequence[dict[str, Any]]] = {}
        for plan, start, count in offsets:
            row = plan["row"]
            if plan["cached"] and str(row["id"]) in marked_now:
                LOGGER.info(
                    "embeddings index skipped one object: marked for recomputation while its batch "
                    "was in flight"
                )
                continue
            resolved: dict[int, bytes] = dict(plan["cached"])
            dims: set[int] = set()
            broken = False
            for position, index in enumerate(plan["missing"][:count]):
                vector = vectors[start + position]
                if not vector:
                    broken = True
                    break
                dims.add(len(vector))
                resolved[index] = pack_vector(vector)
            if broken or len(resolved) != len(plan["texts"]):
                continue
            # A backend that changes dimension mid-batch would poison the index.
            cached_dims = {len(unpack_vector(plan["cached"][index])) for index in plan["cached"]}
            if len(dims | cached_dims) != 1:
                if dims and cached_dims - dims:
                    # The model now answers in a different dimension under the same
                    # name. The reuse cache would resurrect the old-dimension vectors
                    # on every tick, so this object could never converge: drop its
                    # stored vectors and let the next tick re-embed it in full.
                    LOGGER.warning(
                        "embeddings dimension changed (%s -> %s); re-embedding one object",
                        sorted(cached_dims),
                        sorted(dims),
                    )
                    await run_blocking(self.storage.delete_knowledge_embedding, str(row["id"]))
                continue
            dim = (dims | cached_dims).pop()
            source_version = int(row.get("version") or 1)
            items.append(
                {
                    "knowledge_object_id": row["id"],
                    "user_id": row["user_id"],
                    "model": model,
                    "dim": dim,
                    "source_version": source_version,
                    "content_hash": plan["hashes"][0],
                    "chunk_scheme": scheme,
                    "vector": resolved[0],
                }
            )
            chunks[str(row["id"])] = [
                {
                    "chunk_index": position,
                    "user_id": row["user_id"],
                    "model": model,
                    "dim": dim,
                    "source_version": source_version,
                    "chunk_scheme": scheme,
                    "start_char": unit[0],
                    "end_char": unit[1],
                    "content_hash": plan["hashes"][position + 1],
                    "vector": resolved[position + 1],
                }
                for position, unit in enumerate(plan["units"])
            ]
        if items:
            # Persisted per group: the whole tick runs under a timeout, so a single
            # write at the very end would lose every group each time it expires.
            await run_blocking(self.storage.upsert_knowledge_vectors, items, chunks)
        return bool(items)

    async def _approval_hygiene(self) -> None:
        """Просроченные согласия гаснут, оборванные исполнения объявляются неизвестными.

        Оба механизма были написаны вместе с самими подтверждениями и оба НЕ
        вызывались ниоткуда — то есть работали ровно в тестах. Просроченное решение
        продолжало бы числиться действующим сколько угодно долго, а исполнение,
        прерванное смертью процесса, навсегда осталось бы в статусе «исполняется»:
        человек не узнал бы, что про его действие никто ничего не знает.

        Сверка НЕ повторяет действие — она лишь называет исход неизвестным. Повтор
        здесь означал бы второй побочный эффект по одному решению.
        """
        expired = await run_blocking(self.storage.expire_action_approvals)
        unknown = await run_blocking(self.storage.reconcile_stale_claims)
        settled = await run_blocking(self._reconcile_uncertain_approvals)
        if expired or unknown or settled:
            LOGGER.info(
                "Approval hygiene: %d expired, %d marked uncertain, %d settled by observation",
                expired,
                unknown,
                settled,
            )

    def _expire_semantic_supervisor_graph_batch(self) -> int:
        """Retire one bounded page only through fresh source/authority checks."""

        # Lazy imports keep the worker package usable by the execution kernel:
        # the assist adapter itself imports that kernel for request-effect
        # staging, so importing the adapter at module load would form a cycle.
        from friday.orchestration.supervisor_assist_graph_adapter import (
            SupervisorAssistGraphAdapter,
        )
        from friday.orchestration.supervisor_assist_production import (
            supervisor_assist_read_only_effect_gate,
        )
        batch = SupervisorAssistGraphAdapter(self.storage).expire_due(
            lifecycle_check=supervisor_assist_read_only_effect_gate,
            limit=100,
        )
        if batch.retained:
            LOGGER.warning(
                "Semantic supervisor graph expiry retained %d graph(s) after current boundary denial",
                len(batch.retained),
            )
        return len(batch.retired)

    async def _semantic_supervisor_graph_expiry(self) -> None:
        """Close expired durable ownership without invoking or replaying capabilities."""

        retired = await run_blocking(self._expire_semantic_supervisor_graph_batch)
        if retired:
            LOGGER.info("Semantic supervisor graph expiry: %d retired", retired)

    def _reconcile_uncertain_approvals(self) -> int:
        """Выяснить наблюдением, случилось ли то, чей исход остался неизвестным.

        Спека v3 §5: «Uncertain side effects require reconciliation, not automatic
        replay». Сверка для ШАГОВ МИССИИ была написана раньше
        (`ExecutiveService._reconcile_uncertain`), а заявки на подтверждение
        оставались висеть неизвестностью навсегда: `mark_action_approval_uncertain`
        честно писала «ждут сверки человеком», и на этом всё заканчивалось.

        Разница между этими двумя путями — только в том, откуда берутся аргументы:
        у шага миссии из чекпойнта, у заявки из `payload_json`. Проверка одна и та
        же — `POSTCONDITIONS`, читающие факт из хранилища.

        Повтора здесь нет: наблюдение устанавливает исход, а не производит его.
        Заявка, чей эффект не случился, становится `failed`, а не `pending`, —
        решение человека уже потрачено, и новое действие потребует нового.

        Заявка, для инструмента которой проверки нет, остаётся `uncertain`: решить
        за человека, чем кончилось необратимое действие, нельзя.
        """
        from friday.execution_kernel import POSTCONDITIONS

        settled = 0
        for approval in self.storage.list_uncertain_approvals(limit=50):
            tool = str(approval.get("tool") or "")
            verifier = POSTCONDITIONS.get(tool)
            if verifier is None:
                continue
            payload = approval.get("payload")
            if not isinstance(payload, dict):
                raw = approval.get("payload_json")
                try:
                    payload = json.loads(raw) if isinstance(raw, str) else {}
                except (TypeError, ValueError):
                    continue
            if not isinstance(payload, dict) or not payload:
                continue
            owner = str(approval.get("user_id") or "")
            try:
                happened, detail = verifier(self.storage, owner, payload)
            except Exception as exc:  # noqa: BLE001 — сверка не должна ронять гигиену
                LOGGER.error("Approval reconciliation failed (%s)", type(exc).__name__)
                continue
            if self.storage.settle_uncertain_approval(
                str(approval.get("id") or ""), owner, happened=happened, detail=detail
            ):
                settled += 1
                LOGGER.info(
                    "Approval reconciled by observation: effect_happened=%s",
                    "yes" if happened else "no",
                )
        return settled

    async def _database_optimize(self) -> None:
        await run_blocking(self.storage.optimize)
        # Completed request receipts are replay fences, not an archive.  Keep the
        # documented 30-day retry window bounded in the running product instead
        # of relying on a maintenance method that was never scheduled.
        await run_blocking(self.storage.idempotency_prune, days=30)
        # Drop expired single-use bridge nonces so the replay cache stays bounded.
        retention = max(60, self.settings.telegram_signature_max_age_sec * 4)
        await run_blocking(self.storage.prune_bridge_nonces, max_age_sec=retention)

    @property
    def max_timeout_sec(self) -> float:
        """Longest per-task thread budget across the registered workers."""
        return self.supervisor.max_timeout_sec

    _PATHS_RELATIVE_KEY = "maintenance:stored_paths_relative"

    def _relativize_stored_paths_once(self) -> None:
        """Один раз переписать абсолютные пути к файлам в относительные.

        Абсолютный путь привязывает архив к машине: замерено — у всех 1671 документа
        лежали абсолютные пути, и после переезда каждый файл отдавал бы 404,
        неотличимый от «файла нет». Приём уже пишет относительные, но это спасает
        только будущие документы; уже записанные надо починить.

        Один раз, под отметкой: проход дешёвый, но бессмысленно гонять его каждый
        старт, а отметка заодно говорит, что починка была.
        """
        try:
            if self.storage.kv_get(self._PATHS_RELATIVE_KEY):
                return
            report = self.storage.relativize_stored_paths(str(self.settings.files_dir))
            self.storage.kv_set(self._PATHS_RELATIVE_KEY, json.dumps(report, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - починка полезная, но не обязательная для старта
            LOGGER.warning("stored path relativization skipped (%s)", type(exc).__name__)
            return
        if report.get("changed"):
            LOGGER.info(
                "stored paths made portable: %d of %d raw objects rewritten",
                int(report["changed"]),
                int(report.get("scanned") or 0),
            )

    def _load_worker_states(self) -> dict[str, dict[str, Any]]:
        """Прошлое состояние задач из `runtime_kv`. Ошибка чтения не должна мешать старту."""
        restored: dict[str, dict[str, Any]] = {}
        try:
            rows = self.storage.kv_list_prefix("workers:health:")
        except Exception:  # noqa: BLE001 - состояние продвинутое, а не обязательное
            return restored
        for row in rows:
            key = str(row.get("key") or "")
            name = key.removeprefix("workers:health:")
            if not name:
                continue
            try:
                value = json.loads(str(row.get("value") or "{}"))
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                restored[name] = value
        return restored

    async def start(self) -> None:
        if not self.settings.workers_enabled or self._run_task is not None:
            return
        self.register_all()
        self._relativize_stored_paths_once()
        self.supervisor.restore(self._load_worker_states())
        self._run_task = asyncio.create_task(self.supervisor.run(), name="jericho-workers")

    async def stop(self) -> None:
        await self.supervisor.stop()
        if self._run_task is not None:
            self._run_task.cancel()
            await asyncio.gather(self._run_task, return_exceptions=True)
            self._run_task = None
