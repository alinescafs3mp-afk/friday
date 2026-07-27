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
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from jericho.config import JerichoSettings
from jericho.retrieval import (
    chunk_scheme,
    knowledge_chunk_units,
    knowledge_search_text,
    pack_vector,
    unpack_vector,
)
from jericho.storage.models import InboxStatus
from jericho.workers._blocking import current_task, in_flight, run_blocking

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
_ADVICE_ENDPOINT_DOWN_AFTER = 3

# Ceiling on the single input that carries an object's whole-document vector. Generous
# next to a passage (default 1200) and far below what an embeddings service will refuse.
_DOC_VECTOR_MAX_CHARS = 20_000

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
_EMBED_REQUEST_MAX_CHARS = 40_000


class WorkerBatchError(RuntimeError):
    """One task completed its tenant sweep with isolated failures."""


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
        if self._state_sink is not None:
            try:
                self._state_sink(task.name, current)
            except Exception:
                LOGGER.debug("Could not persist worker state for %s", task.name, exc_info=True)

    async def run(self) -> None:
        if self._running:
            return
        self._running = True
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
        LOGGER.info("Worker supervisor stopped")

    async def _run_task(self, task: IntervalTask) -> None:
        if not task.run_immediately:
            self._publish(
                task,
                status="scheduled",
                next_run_at=(datetime.now(UTC) + timedelta(seconds=task.interval_sec)).isoformat(
                    timespec="seconds"
                ),
                consecutive_failures=0,
            )
            await asyncio.sleep(task.interval_sec)
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
                LOGGER.exception("Worker %s failed", task.name)
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
        vault.sync_object({**item, "_entity_names": links.get(str(item.get("id") or ""), [])})


class WorkersManager:
    def __init__(
        self,
        settings: JerichoSettings,
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
        self.supervisor.register(
            "memory_vault_sync",
            self._vault_sync_all,
            300,
            enabled=self.memory_vault is not None,
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
        except Exception:
            # Journalling must never take down the worker it is observing.
            LOGGER.debug("Could not record worker transition for %s", name, exc_info=True)

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

    async def _for_each_user(self, operation: Callable[[str], Awaitable[Any]]) -> None:
        failures = 0
        for user_id in self.storage.list_user_ids(active_only=True):
            try:
                await operation(user_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                LOGGER.exception("Worker operation failed for tenant %s", user_id)
            await asyncio.sleep(0)
        if failures:
            raise WorkerBatchError(f"{failures} tenant operation(s) failed")

    async def _retrieval_eval_all(self) -> None:
        await self._for_each_user(self._retrieval_eval)

    async def _retrieval_eval(self, user_id: str) -> None:
        if not self.storage.list_eval_cases(user_id):
            return
        from jericho.eval import run_eval

        # Prune before measuring: a case whose expected objects were all deleted can
        # never be satisfied and would depress recall for good. Mined rows only —
        # a hand-curated case is never touched, however stale it looks.
        hygiene = await run_blocking(self.storage.prune_eval_cases, user_id)
        report = await run_eval(self.storage, self.embeddings, self.settings, user_id, k=self.settings.eval_k)
        if isinstance(report.get("gold_set"), dict):
            report["gold_set"]["pruned"] = hygiene
        self.storage.kv_set(f"eval:last_report:{user_id}", json.dumps(report, ensure_ascii=False))

    async def _knowledge_dedup_all(self) -> None:
        # The tick budget is split across tenants, so no single scan can run the whole
        # tick past the supervisor's timeout — asyncio.to_thread is not interruptible,
        # and an over-long scan would show up as a recurring `timeout` in worker health
        # while quietly still doing the work.
        tenants = max(1, len(await run_blocking(self.storage.list_user_ids, active_only=True)))
        share = max(1.0, float(self.settings.dedup_scan_max_seconds) / tenants)
        await self._for_each_user(functools.partial(self._knowledge_dedup, max_seconds=share))

    async def _knowledge_dedup(self, user_id: str, *, max_seconds: float | None = None) -> None:
        from jericho.dedup import detect_near_duplicates

        result = await run_blocking(
            functools.partial(detect_near_duplicates, max_seconds=max_seconds),
            self.storage,
            self.settings,
            user_id,
        )
        if result.get("detected"):
            LOGGER.info("Near-duplicate scan for tenant %s: %d proposal(s)", user_id, result["detected"])
        if result.get("incomplete"):
            LOGGER.info(
                "Near-duplicate scan for tenant %s paused with %d object(s) pending; resumes next tick",
                user_id,
                int(result.get("pending") or 0),
            )

    async def _entity_resolution_all(self) -> None:
        await self._for_each_user(self._entity_resolution)

    async def _entity_resolution(self, user_id: str) -> None:
        candidates = await run_blocking(self.kg.resolver.detect_duplicates, user_id, min_confidence=0.60)
        if candidates:
            LOGGER.info("Suggested %d potential entity merges for tenant %s", len(candidates), user_id)

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
        self.storage.kv_set(
            f"workers:knowledge_quality:{user_id}",
            json.dumps(summary, ensure_ascii=False, sort_keys=True),
        )
        if candidates:
            LOGGER.info(
                "Quality review found %d conservative candidates for tenant %s",
                len(candidates),
                user_id,
            )

    async def _inbox_model_advice_all(self) -> None:
        await self._for_each_user(self._inbox_model_advice)

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
        for item in self.storage.list_inbox_detailed(
            user_id,
            InboxStatus.PENDING,
            limit=50,
        ):
            try:
                result = await self.ingestion.advise_inbox_item(
                    user_id,
                    item["id"],
                    llm=self.llm,
                    requested_by="worker:inbox_model_advice",
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                consecutive_failures += 1
                LOGGER.exception(
                    "Inbox model advice failed for tenant %s item %s",
                    user_id,
                    item.get("id"),
                )
                if consecutive_failures >= _ADVICE_ENDPOINT_DOWN_AFTER:
                    # Isolated failures are per-item and worth stepping over; this many
                    # in a row is the endpoint, not the documents. Continuing would run
                    # the remaining items through three LLM retries each and exhaust the
                    # worker's whole budget for nothing — observed here as 46 failures
                    # and an eight-minute timeout while the endpoint was down. Stopping
                    # also stops hammering something that is already struggling.
                    LOGGER.warning(
                        "Inbox model advice stopped after %d consecutive failures for tenant %s; "
                        "treating the model endpoint as unavailable",
                        consecutive_failures,
                        user_id,
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
            LOGGER.info("Added local-model advice to %d Inbox items for tenant %s", processed, user_id)
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
        self.storage.kv_set(
            f"workers:lifecycle:{user_id}",
            json.dumps(report, ensure_ascii=False, sort_keys=True),
        )
        if candidates:
            LOGGER.info(
                "Lifecycle review proposed %d candidates for tenant %s; no changes applied",
                len(candidates),
                user_id,
            )

    async def _vault_sync_all(self) -> None:
        await self._for_each_user(self._vault_sync)

    async def _vault_sync(self, user_id: str) -> None:
        if self.memory_vault is None:
            return
        offset = 0
        live: set[str] = set()
        vault = self.memory_vault
        while True:
            objects = await run_blocking(
                self.storage.list_knowledge_objects, user_id, limit=250, offset=offset
            )
            if not objects:
                break
            # One page per offload, not one file. `sync_object` is mkstemp + write +
            # **fsync** + os.replace for every object, and the loop had no `await` in
            # it at all, so a whole-corpus re-render ran on the event loop every 300
            # seconds — the fsyncs alone stall it for as long as the disk takes.
            await run_blocking(_sync_vault_page, vault, self.storage, objects)
            live.update(str(item.get("id") or "") for item in objects)
            offset += len(objects)
            if len(objects) < 250:
                break
        # Only after the paging loop has run to completion: `live` is then the whole
        # live set, and pruning against a partial one would delete valid notes. An
        # exception inside the loop propagates and skips this entirely.
        #
        # Without it the vault only ever grew. `list_knowledge_objects` filters
        # `deleted_at IS NULL` and `delete_object` was called from the hard-purge path
        # alone, so a soft-deleted or IGNORED object kept a plaintext copy of its full
        # content on disk forever — while the user was told it was deleted and search
        # agreed with them.
        removed = await run_blocking(self.memory_vault.prune_orphans, user_id, live)
        if removed:
            LOGGER.info("Vault sync removed %d note(s) for objects that are no longer live", removed)

    async def _scheduled_backup(self) -> None:
        raw = self.storage.kv_get("workers:last_backup_at")
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
        self.storage.kv_set("workers:last_backup_at", now.isoformat(timespec="seconds"))
        self.storage.kv_set("workers:last_backup", json.dumps(result, ensure_ascii=False))
        LOGGER.info("Scheduled database backup created: %s", result.get("database"))
        # Every backup, not just failures: "when did this last actually run" is the
        # question asked after something has already gone wrong, and by then the log
        # that would have answered it has usually rotated.
        self.storage.record_event(
            "backup.created",
            {
                "database": result.get("database"),
                "size_bytes": result.get("size_bytes"),
                "integrity_check": result.get("integrity_check"),
                "label": "scheduled",
            },
        )
        # A same-disk backup is not a real backup: mirror it offsite when
        # configured (encrypted when a key file is set).
        from jericho.backup_mirror import mirror_backups

        mirror = await run_blocking(mirror_backups, self.settings)
        if mirror.get("enabled"):
            # Stamped so diagnostics can tell "mirrored a moment ago" from
            # "mirrored last month and the disk has been unplugged since".
            mirror["reported_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            self.storage.kv_set("workers:last_backup_mirror", json.dumps(mirror, ensure_ascii=False))
        # Prune AFTER mirroring, so an offsite copy of the oldest generation is made
        # before the local one goes. Retention is the missing half of a daily backup:
        # without it the disk fills, and it takes the live instance with it.
        pruned = await run_blocking(self.storage.prune_backups, keep=self.settings.backup_keep)
        if pruned.get("removed"):
            self.storage.record_event("backup.pruned", pruned)

    async def _embeddings_index_all(self) -> None:
        """Embed a bounded batch of Knowledge Objects lacking a current vector.

        The batch spans all tenants: vectors depend only on object content, so the
        index reconciles the whole corpus and newly promoted or re-enriched objects
        become dense-searchable within one cycle. A backend outage simply defers the
        batch to the next tick — nothing is dropped.
        """
        if self.embeddings is None or not getattr(self.embeddings, "remote_enabled", False):
            return
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
            return

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
        for row in rows:
            units = knowledge_chunk_units(
                row,
                max_chars=self.settings.embeddings_chunk_chars,
                overlap_chars=self.settings.embeddings_chunk_overlap_chars,
                max_chunks=max_chunks,
            )
            # Bounded before it is sent. The whole-object vector is a COARSE signal —
            # passage vectors carry the detail — but it goes as one input, and a large
            # object makes that input alone exceed what an embeddings service will take.
            # Measured: 104175 characters timed out where 20000 answered in seconds.
            # Worse, the object's inputs travel in one request, so an oversized document
            # lost its passages too and ended up with NO vector at all — appearing in
            # the log as "backend returned no usable vectors" rather than as too long.
            doc_text = knowledge_search_text(row)[:_DOC_VECTOR_MAX_CHARS]
            plans.append({"row": row, "doc_text": doc_text, "units": units})

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
        shared = await run_blocking(
            self.storage.get_vectors_by_content_hash,
            [digest for plan in plans for digest in plan["hashes"]],
            model,
        )
        groups: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_size = 0
        for plan in plans:
            texts = plan["texts"]
            hashes = plan["hashes"]
            known = {**shared, **reusable.get(str(plan["row"]["id"]), {})}
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
        for group in groups:
            if not await self._embed_group(group, model, scheme):
                continue
            indexed += len(group)
        if indexed:
            LOGGER.info("embeddings index updated %d objects", indexed)

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

        items: list[dict[str, Any]] = []
        chunks: dict[str, Sequence[dict[str, Any]]] = {}
        for plan, start, count in offsets:
            row = plan["row"]
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
                        "embeddings dimension changed for %s (%s -> %s); re-embedding it",
                        row["id"],
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

    async def _database_optimize(self) -> None:
        await run_blocking(self.storage.optimize)
        # Drop expired single-use bridge nonces so the replay cache stays bounded.
        retention = max(60, self.settings.telegram_signature_max_age_sec * 4)
        await run_blocking(self.storage.prune_bridge_nonces, max_age_sec=retention)

    @property
    def max_timeout_sec(self) -> float:
        """Longest per-task thread budget across the registered workers."""
        return self.supervisor.max_timeout_sec

    async def start(self) -> None:
        if not self.settings.workers_enabled or self._run_task is not None:
            return
        self.register_all()
        self._run_task = asyncio.create_task(self.supervisor.run(), name="jericho-workers")

    async def stop(self) -> None:
        await self.supervisor.stop()
        if self._run_task is not None:
            self._run_task.cancel()
            await asyncio.gather(self._run_task, return_exceptions=True)
            self._run_task = None
