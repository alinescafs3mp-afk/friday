"""Supervised background enrichment and maintenance workers.

Workers iterate over every active tenant and never merge entities or classify
uncertain inbox items without a user decision.  Failures are isolated per task,
so one bad document or tenant cannot stop lifecycle, backup, or graph work.
"""

from __future__ import annotations

import asyncio
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

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntervalTask:
    name: str
    func: Callable[[], Awaitable[Any]]
    interval_sec: float
    enabled: bool = True
    run_immediately: bool = True
    timeout_sec: float = 300.0


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
            elapsed = time.monotonic() - started
            delay = max(1.0, task.interval_sec - elapsed)
            self._publish(
                task,
                next_run_at=(datetime.now(UTC) + timedelta(seconds=delay)).isoformat(timespec="seconds"),
            )
            await asyncio.sleep(delay)


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

    def _persist_worker_state(self, name: str, state: dict[str, Any]) -> None:
        self.storage.kv_set(
            f"workers:health:{name}",
            json.dumps(state, ensure_ascii=False, sort_keys=True),
        )

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

        report = await run_eval(self.storage, self.embeddings, self.settings, user_id, k=self.settings.eval_k)
        self.storage.kv_set(f"eval:last_report:{user_id}", json.dumps(report, ensure_ascii=False))

    async def _knowledge_dedup_all(self) -> None:
        await self._for_each_user(self._knowledge_dedup)

    async def _knowledge_dedup(self, user_id: str) -> None:
        from jericho.dedup import detect_near_duplicates

        result = await asyncio.to_thread(detect_near_duplicates, self.storage, self.settings, user_id)
        if result.get("detected"):
            LOGGER.info("Near-duplicate scan for tenant %s: %d proposal(s)", user_id, result["detected"])

    async def _entity_resolution_all(self) -> None:
        await self._for_each_user(self._entity_resolution)

    async def _entity_resolution(self, user_id: str) -> None:
        candidates = self.kg.resolver.detect_duplicates(user_id, min_confidence=0.60)
        if candidates:
            LOGGER.info("Suggested %d potential entity merges for tenant %s", len(candidates), user_id)

    async def _knowledge_quality_scan_all(self) -> None:
        await self._for_each_user(self._knowledge_quality_scan)

    async def _knowledge_quality_scan(self, user_id: str) -> None:
        """Refresh a read-only quality report; never mutate or hide knowledge automatically."""

        candidates = await asyncio.to_thread(
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
                LOGGER.exception(
                    "Inbox model advice failed for tenant %s item %s",
                    user_id,
                    item.get("id"),
                )
                continue
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
        candidates = await asyncio.to_thread(
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
        while True:
            objects = self.storage.list_knowledge_objects(user_id, limit=250, offset=offset)
            if not objects:
                break
            for item in objects:
                self.memory_vault.sync_object(item)
            offset += len(objects)
            if len(objects) < 250:
                break

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
        result = await asyncio.to_thread(self.storage.create_backup, label="scheduled")
        self.storage.kv_set("workers:last_backup_at", now.isoformat(timespec="seconds"))
        self.storage.kv_set("workers:last_backup", json.dumps(result, ensure_ascii=False))
        LOGGER.info("Scheduled database backup created: %s", result.get("database"))
        # A same-disk backup is not a real backup: mirror it offsite when
        # configured (encrypted when a key file is set).
        from jericho.backup_mirror import mirror_backups

        mirror = await asyncio.to_thread(mirror_backups, self.settings)
        if mirror.get("enabled"):
            self.storage.kv_set("workers:last_backup_mirror", json.dumps(mirror, ensure_ascii=False))

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
        rows = await asyncio.to_thread(
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
            doc_text = knowledge_search_text(row)
            plans.append({"row": row, "doc_text": doc_text, "units": units})

        reusable = await asyncio.to_thread(
            self.storage.get_reusable_vectors, [str(plan["row"]["id"]) for plan in plans], model
        )
        groups: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_size = 0
        for plan in plans:
            texts = [plan["doc_text"], *(unit[2] for unit in plan["units"])]
            hashes = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts]
            known = reusable.get(str(plan["row"]["id"]), {})
            plan["texts"] = texts
            plan["hashes"] = hashes
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
            returned = await self.embeddings.embed(flattened)  # type: ignore[union-attr]
            if not returned or len(returned) != len(flattened):
                LOGGER.warning(
                    "embeddings index skipped %d objects: backend returned no usable vectors",
                    len(group),
                )
                return False
            vectors = list(returned)

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
                    await asyncio.to_thread(self.storage.delete_knowledge_embedding, str(row["id"]))
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
            await asyncio.to_thread(self.storage.upsert_knowledge_vectors, items, chunks)
        return bool(items)

    async def _database_optimize(self) -> None:
        await asyncio.to_thread(self.storage.optimize)
        # Drop expired single-use bridge nonces so the replay cache stays bounded.
        retention = max(60, self.settings.telegram_signature_max_age_sec * 4)
        await asyncio.to_thread(self.storage.prune_bridge_nonces, max_age_sec=retention)

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
