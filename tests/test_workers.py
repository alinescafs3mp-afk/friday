from __future__ import annotations

import asyncio
import json
from hashlib import sha256

import pytest

from friday.ingestion import IngestionPipeline
from friday.ingestion._base import _PROMOTION_POLICY_VERSION
from friday.knowledge_graph import KnowledgeGraph
from friday.secondary_brain.contracts import SecondaryMode
from friday.secondary_product_witness import (
    secondary_product_witness_content,
    secondary_product_witness_source_ref,
)
from friday.storage.models import (
    Entity,
    EntityType,
    InboxItem,
    KnowledgeObject,
    RawObject,
    new_id,
)
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
        def list_inbox_advice_candidates(user_id, **kwargs):
            del user_id, kwargs
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
    """A full current page cannot hide eligible, public, non-witness tail rows."""

    storage.ensure_user("alice")

    def store_item(
        source_ref: str,
        content: str,
        *,
        promotion_score: float,
        suggestions: dict | None = None,
        created_at: str = "2026-01-01T00:00:00+00:00",
        user_id: str = "alice",
    ) -> tuple[RawObject, InboxItem]:
        raw = storage.store_raw_object(
            RawObject(
                id=new_id("raw"),
                user_id=user_id,
                source="api",
                source_ref=source_ref,
                raw_content=content,
                content_type="text/plain",
            )
        )
        item = storage.store_inbox_item(
            InboxItem(
                id=new_id("inbox"),
                user_id=user_id,
                raw_object_id=raw.id,
                promotion_score=promotion_score,
                suggestions_json=suggestions or {},
                created_at=created_at,
            )
        )
        return raw, item

    primary_alias = "primary-inbox-model"
    secondary_alias = "secondary-inbox-model"
    current_sample_ids: list[str] = []
    for index in range(1002):
        model = primary_alias if index % 2 == 0 else secondary_alias
        _raw, item = store_item(
            f"already-current:{index}",
            f"already advised material {index}",
            promotion_score=1.0,
            suggestions={
                "model_advice": {
                    "policy_version": _PROMOTION_POLICY_VERSION,
                    "model": model,
                }
            },
        )
        if index < 2:
            current_sample_ids.append(item.id)

    capped_ids: list[str] = []
    for index in range(8):
        _raw, item = store_item(
            f"attempt-capped:{index}",
            f"failed advice material {index}",
            promotion_score=0.95,
            suggestions={"model_advice_failures": 3},
        )
        capped_ids.append(item.id)

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

    private_raw, private_item = store_item(
        "private:advice-candidate",
        "PRIVATE-ADVICE-CANARY",
        promotion_score=1.0,
    )
    private_knowledge = storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=private_raw.id,
            content=private_raw.raw_content,
            content_type="text",
            title="private advice dependency",
        )
    )
    private_entity = storage.create_entity(
        Entity(
            id=new_id("ent"),
            user_id="alice",
            name=f"Private advice entity {private_raw.id}",
            entity_type=EntityType.EVENT,
        )
    )
    storage.link_knowledge_entity(
        "alice",
        private_knowledge.id,
        private_entity.id,
        status="accepted",
    )
    with storage.transaction() as connection:
        connection.execute(
            """INSERT INTO private_entity_owners(
                   entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (private_entity.id, "another-person", "2026-09-01T00:00:00+00:00"),
        )

    candidate_ids = [
        store_item(
            f"eligible:{index}",
            f"Идея: использовать Redis для кеша сервера, вариант {index}",
            promotion_score=(0.1, 0.9, 0.5, 0.7)[index],
            created_at="2026-02-01T00:00:00+00:00",
        )[1].id
        for index in range(4)
    ]
    malformed_candidate_id = candidate_ids[0]
    eligible_candidate_ids = candidate_ids[1:]
    eligible_candidate_order = sorted(eligible_candidate_ids)
    with storage.transaction() as connection:
        connection.execute(
            "UPDATE inbox SET suggestions_json=? WHERE id=?",
            ("{malformed-json", malformed_candidate_id),
        )
        connection.execute(
            "UPDATE inbox SET suggestions_json=? WHERE id=?",
            (
                json.dumps({"model_advice_failures": "not-a-number"}),
                eligible_candidate_ids[0],
            ),
        )

    class FakeLLM:
        enabled = True
        model = primary_alias

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, _messages, **_kwargs):
            self.calls += 1
            return {
                "content": json.dumps(
                    {
                        "title": "Redis cache proposal",
                        "summary": "Рассмотреть Redis как кеш сервера.",
                        "knowledge_kind": "technical_note",
                        "importance": 0.6,
                        "tags": ["redis"],
                        "entities": [],
                        "recommended_action": "review",
                        "confidence": 0.8,
                        "rationale": "Техническая идея требует решения владельца.",
                    },
                    ensure_ascii=False,
                )
            }

    class ShadowSecondary:
        mode = SecondaryMode.SHADOW
        served_model_alias = secondary_alias
        advisory_profile_limits = None

        @staticmethod
        async def run_shadow(_request_factory, primary_call, *, validator):
            del validator
            return await primary_call()

    llm = FakeLLM()
    graph = KnowledgeGraph(storage)

    class CancellingIngestion:
        secondary_brain = ShadowSecondary()

        def __init__(self) -> None:
            self.selected = ""

        async def advise_inbox_item(self, _user_id, item_id, **_kwargs):
            self.selected = item_id
            raise asyncio.CancelledError

    cancelling = CancellingIngestion()
    cancelled_manager = WorkersManager(settings, storage, cancelling, kg=graph, llm=llm)
    with pytest.raises(asyncio.CancelledError):
        await cancelled_manager._inbox_model_advice("alice")  # noqa: SLF001
    assert cancelling.selected == eligible_candidate_order[0], {
        "selected": cancelling.selected,
        "insertion_order": candidate_ids,
    }
    assert not any("cursor" in name or "offset" in name for name in vars(cancelled_manager))

    ingestion = IngestionPipeline(
        settings,
        storage,
        graph,
        llm,
        secondary_brain=ShadowSecondary(),
    )
    manager = WorkersManager(settings, storage, ingestion, kg=graph, llm=llm)
    await manager._inbox_model_advice("alice")  # noqa: SLF001

    assert llm.calls == 2, "the worker must retain the per-cycle two-success cap"
    advised = {
        item_id
        for item_id in eligible_candidate_ids
        if (stored := storage.get_inbox_item(item_id, "alice"))
        and json.loads(str(stored["suggestions_json"] or "{}")).get("model_advice")
    }
    assert advised == set(eligible_candidate_order[:2])
    retained_models = {
        json.loads(str(storage.get_inbox_item(item_id, "alice")["suggestions_json"]))["model_advice"]["model"]
        for item_id in current_sample_ids
    }
    assert retained_models == {primary_alias, secondary_alias}
    assert all(
        json.loads(str(storage.get_inbox_item(item_id, "alice")["suggestions_json"]))["model_advice_failures"]
        == 3
        for item_id in capped_ids
    )

    with storage.transaction() as connection:
        connection.execute(
            "UPDATE inbox SET promotion_score=0.0 WHERE id=?",
            (eligible_candidate_order[2],),
        )
    new_high_ranked_ids = [
        store_item(
            f"new-high-ranked:{index}",
            f"Идея: новый Redis материал {index}",
            promotion_score=1.0,
            created_at="2026-02-01T00:00:01+00:00",
        )[1].id
        for index in range(3)
    ]
    restarted_manager = WorkersManager(settings, storage, ingestion, kg=graph, llm=llm)
    await restarted_manager._inbox_model_advice("alice")  # noqa: SLF001
    assert llm.calls == 4
    remaining_old = eligible_candidate_order[2]
    assert json.loads(str(storage.get_inbox_item(remaining_old, "alice")["suggestions_json"])).get(
        "model_advice"
    )
    newly_advised = {
        item_id
        for item_id in new_high_ranked_ids
        if json.loads(str(storage.get_inbox_item(item_id, "alice")["suggestions_json"])).get("model_advice")
    }
    assert len(newly_advised) == 1
    untouched = {
        row["id"]: json.loads(str(row["suggestions_json"] or "{}"))
        for row in storage.execute(
            "SELECT id, suggestions_json FROM inbox WHERE id IN (?, ?)",
            (witness.id, private_item.id),
        ).fetchall()
    }
    assert not untouched[witness.id].get("model_advice")
    assert not untouched[private_item.id].get("model_advice")
    malformed_row = storage.execute(
        "SELECT suggestions_json FROM inbox WHERE id=?",
        (malformed_candidate_id,),
    ).fetchone()
    assert malformed_row["suggestions_json"] == "{malformed-json"
    assert storage.get_inbox_item(witness.id, "alice") is not None

    poison_owner = "poison-owner"
    storage.ensure_user(poison_owner)
    poison_ids = [
        store_item(
            f"poison-counter:{index}",
            f"Идея: использовать Redis после poison counter {index}",
            promotion_score=1.0,
            suggestions={"model_advice_failures": "3_0" if index % 2 == 0 else "٣"},
            created_at="2026-03-01T00:00:00+00:00",
            user_id=poison_owner,
        )[1].id
        for index in range(50)
    ]
    with storage.transaction() as connection:
        connection.execute(
            "UPDATE inbox SET suggestions_json=? WHERE id=?",
            ('{"model_advice_failures":-1e999}', poison_ids[-1]),
        )
    poison_tail = store_item(
        "poison-counter:eligible-tail",
        "Идея: использовать Redis в хвосте после non-canonical counters",
        promotion_score=0.0,
        created_at="2026-03-01T00:00:01+00:00",
        user_id=poison_owner,
    )[1].id
    for _cycle in range(25):
        calls_before = llm.calls
        fresh_manager = WorkersManager(settings, storage, ingestion, kg=graph, llm=llm)
        await fresh_manager._inbox_model_advice(poison_owner)  # noqa: SLF001
        assert llm.calls - calls_before == 2
    assert not json.loads(str(storage.get_inbox_item(poison_tail, poison_owner)["suggestions_json"])).get(
        "model_advice"
    )

    calls_before = llm.calls
    fresh_manager = WorkersManager(settings, storage, ingestion, kg=graph, llm=llm)
    await fresh_manager._inbox_model_advice(poison_owner)  # noqa: SLF001
    assert llm.calls - calls_before == 1
    assert json.loads(str(storage.get_inbox_item(poison_tail, poison_owner)["suggestions_json"])).get(
        "model_advice"
    )
    assert all(
        json.loads(str(storage.get_inbox_item(item_id, poison_owner)["suggestions_json"])).get("model_advice")
        for item_id in poison_ids
    )


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
