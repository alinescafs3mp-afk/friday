from __future__ import annotations

import asyncio
from hashlib import sha256

import pytest

from friday.secondary_product_witness import (
    secondary_product_witness_content,
    secondary_product_witness_source_ref,
)
from friday.storage.models import InboxItem, RawObject, new_id
from friday.workers import IntervalTask, WorkerBatchError, WorkersManager, WorkerSupervisor


@pytest.mark.asyncio
async def test_worker_supervisor_persists_timeout_without_crashing_scheduler():
    published: list[tuple[str, dict]] = []

    async def slow_task() -> None:
        await asyncio.sleep(1)

    supervisor = WorkerSupervisor(lambda name, state: published.append((name, dict(state))))
    task = IntervalTask(
        name="slow",
        func=slow_task,
        interval_sec=1.0,
        timeout_sec=0.02,
    )
    supervisor._running = True  # noqa: SLF001 - focused scheduler regression
    handle = asyncio.create_task(supervisor._run_task(task))  # noqa: SLF001
    supervisor._handles = [handle]  # noqa: SLF001
    try:
        for _ in range(100):
            if supervisor.snapshot().get("slow", {}).get("status") == "timeout":
                break
            await asyncio.sleep(0.005)
        state = supervisor.snapshot()["slow"]
        assert state["status"] == "timeout"
        assert state["consecutive_failures"] == 1
        assert state["error_type"] == "TimeoutError"
        assert any(item[1].get("status") == "running" for item in published)
        assert any(item[1].get("status") == "timeout" for item in published)
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_inbox_advice_isolates_items_but_reports_degraded_batch(settings):
    class FakeStorage:
        @staticmethod
        def list_inbox_detailed(user_id, status, limit):
            del user_id, status, limit
            return [{"id": "bad"}, {"id": "good"}]

    class FakeIngestion:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def advise_inbox_item(self, user_id, item_id, **kwargs):
            del user_id, kwargs
            self.calls.append(item_id)
            if item_id == "bad":
                raise ValueError("malformed item")
            return {"idempotent_replay": False}

    class FakeLLM:
        enabled = True

    ingestion = FakeIngestion()
    manager = WorkersManager(
        settings,
        FakeStorage(),
        ingestion,
        kg=object(),
        llm=FakeLLM(),
    )

    with pytest.raises(WorkerBatchError, match="1 Inbox advice item"):
        await manager._inbox_model_advice("alice")  # noqa: SLF001

    assert ingestion.calls == ["bad", "good"]


@pytest.mark.asyncio
async def test_inbox_advice_query_never_selects_reserved_product_witness(settings, storage):
    storage.ensure_user("alice")
    ordinary_ids: list[str] = []
    for index in range(2):
        raw = storage.store_raw_object(
            RawObject(
                id=new_id("raw"),
                user_id="alice",
                source="api",
                source_ref=f"ordinary:{index}",
                raw_content=f"ordinary material {index}",
                content_type="text/plain",
            )
        )
        item = storage.store_inbox_item(InboxItem(id=new_id("inbox"), user_id="alice", raw_object_id=raw.id))
        ordinary_ids.append(item.id)

    nonce = "b" * 32
    source_ref = secondary_product_witness_source_ref("assist", nonce)
    content = secondary_product_witness_content("assist", nonce)
    witness_raw = storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="api",
            source_ref=source_ref,
            raw_content=content,
            content_type="text/plain",
            content_hash=sha256(content.encode()).hexdigest(),
            metadata_json={"secondary_product_witness": True, "uploaded_by": "alice"},
        )
    )
    witness = storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id="alice",
            raw_object_id=witness_raw.id,
            promotion_score=1.0,
        )
    )

    class FakeIngestion:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def advise_inbox_item(self, _user_id, item_id, **_kwargs):
            self.calls.append(item_id)
            return {"idempotent_replay": False}

    class FakeLLM:
        enabled = True

    ingestion = FakeIngestion()
    manager = WorkersManager(settings, storage, ingestion, kg=object(), llm=FakeLLM())
    await manager._inbox_model_advice("alice")  # noqa: SLF001

    assert set(ingestion.calls) == set(ordinary_ids)
    assert len(ingestion.calls) == 2
    assert witness.id not in ingestion.calls
    assert storage.get_inbox_item(witness.id, "alice") is not None


@pytest.mark.asyncio
async def test_database_maintenance_bounds_completed_idempotency_receipts(settings):
    class FakeStorage:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int | None]] = []

        def optimize(self) -> None:
            self.calls.append(("optimize", None))

        def idempotency_prune(self, *, days: int) -> int:
            self.calls.append(("idempotency_prune", days))
            return 2

        def prune_bridge_nonces(self, *, max_age_sec: int) -> int:
            self.calls.append(("prune_bridge_nonces", max_age_sec))
            return 1

    storage = FakeStorage()
    manager = WorkersManager(settings, storage, ingestion=None, kg=None)

    await manager._database_optimize()  # noqa: SLF001

    assert storage.calls == [
        ("optimize", None),
        ("idempotency_prune", 30),
        ("prune_bridge_nonces", max(60, settings.telegram_signature_max_age_sec * 4)),
    ]
