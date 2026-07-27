from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from PIL import Image

from jericho.ingestion import IngestionPipeline
from jericho.knowledge_graph import KnowledgeGraph
from jericho.storage import SCHEMA_VERSION
from jericho.storage.models import (
    FeedbackItem,
    FeedbackType,
    InboxStatus,
    KnowledgeObject,
    RawObject,
    new_id,
)


def _store_knowledge(
    storage,
    user_id: str,
    content: str,
    *,
    title: str,
    importance: float = 0.2,
    quality: float = 0.25,
    promotion: float = 0.25,
    metadata: dict | None = None,
) -> dict:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("source"),
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=content,
        content_type="text",
        title=title,
        summary=content,
        importance=importance,
        quality_score=quality,
        promotion_score=promotion,
        metadata_json=metadata or {},
    )
    storage.store_knowledge_object(ko)
    return storage.get_knowledge_object(ko.id, user_id) or {}


@pytest.mark.asyncio
async def test_graph_evolution_is_useful_but_review_only(settings, storage):
    graph = KnowledgeGraph(storage)
    pipeline = IngestionPipeline(settings, storage, graph)

    first = await pipeline.ingest_text(
        "alice",
        "Запомни: проект Orion использует PostgreSQL 16.",
        source_ref="relation:orion-postgres",
    )
    assert first["promoted"] is True
    candidates = storage.list_relation_candidates("alice", status="suggested")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["relation_type"] == "uses"
    assert candidate["source_name"] == "Orion"
    assert candidate["target_name"] == "PostgreSQL"
    assert (
        storage.execute(
            "SELECT COUNT(*) AS count FROM relations WHERE user_id=? AND relation_type='uses'",
            ("alice",),
        ).fetchone()["count"]
        == 0
    )

    reviewed = graph.review_relation_candidate(
        "alice",
        candidate["id"],
        "accepted",
        reviewed_by="alice",
    )
    assert reviewed and reviewed["status"] == "accepted"
    assert (
        storage.execute(
            "SELECT COUNT(*) AS count FROM relations WHERE user_id=? AND relation_type='uses'",
            ("alice",),
        ).fetchone()["count"]
        == 1
    )

    old = await pipeline.ingest_text(
        "alice",
        "Запомни: сервер Atlas имеет IP 10.0.0.5.",
        source_ref="conflict:atlas:old",
    )
    new = await pipeline.ingest_text(
        "alice",
        "Запомни: сервер Atlas имеет IP 10.0.0.7.",
        source_ref="conflict:atlas:new",
    )
    conflicts = storage.list_knowledge_conflicts("alice", status="suggested")
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["conflict_type"] == "address_mismatch"
    assert storage.get_knowledge_object(old["knowledge_object"]["id"], "alice")["deleted_at"] is None
    assert storage.get_knowledge_object(new["knowledge_object"]["id"], "alice")["deleted_at"] is None

    reviewed_conflict = graph.review_conflict(
        "alice",
        conflict["id"],
        "confirmed",
        reviewed_by="alice",
        resolution_note="Needs human verification",
    )
    assert reviewed_conflict and reviewed_conflict["status"] == "confirmed"
    with pytest.raises(ValueError, match="only confirmed conflicts may advance to resolved"):
        graph.review_conflict(
            "alice",
            conflict["id"],
            "dismissed",
            reviewed_by="alice",
        )
    resolved_conflict = graph.review_conflict(
        "alice",
        conflict["id"],
        "resolved",
        reviewed_by="alice",
        resolution_note="The current source was corrected explicitly",
    )
    assert resolved_conflict and resolved_conflict["status"] == "resolved"
    with pytest.raises(ValueError, match="only confirmed conflicts may advance to resolved"):
        graph.review_conflict(
            "alice",
            conflict["id"],
            "confirmed",
            reviewed_by="alice",
        )
    # Reviewing a contradiction records the decision but never rewrites either source claim.
    assert "10.0.0.5" in storage.get_knowledge_object(old["knowledge_object"]["id"], "alice")["content"]
    assert "10.0.0.7" in storage.get_knowledge_object(new["knowledge_object"]["id"], "alice")["content"]


def test_feedback_state_replaces_rating_and_updates_usage_attribution(storage):
    ko = _store_knowledge(storage, "alice", "Atlas uses PostgreSQL.", title="Atlas stack")
    context = {"knowledge_object_ids": [ko["id"]], "interaction_mode": "knowledge_work"}

    storage.store_feedback(
        FeedbackItem(
            id=new_id("feedback"),
            user_id="alice",
            target_type="answer",
            target_id="msg-1",
            feedback_type=FeedbackType.ANSWER_USEFULNESS,
            score=1.0,
            context_json=context,
        )
    )
    usage = storage.get_knowledge_usage("alice", [ko["id"]])[ko["id"]]
    assert usage["positive_feedback_count"] == 1
    assert usage["negative_feedback_count"] == 0

    storage.store_feedback(
        FeedbackItem(
            id=new_id("feedback"),
            user_id="alice",
            target_type="answer",
            target_id="msg-1",
            feedback_type=FeedbackType.ANSWER_USEFULNESS,
            score=-1.0,
            context_json=context,
        )
    )
    state = storage.get_feedback_state(
        "alice",
        target_type="answer",
        target_id="msg-1",
        feedback_type=FeedbackType.ANSWER_USEFULNESS.value,
    )
    assert len(state) == 1
    assert state[0]["score"] == -1.0
    assert len(storage.get_feedback_for_target("alice", "answer", "msg-1")) == 2
    usage = storage.get_knowledge_usage("alice", [ko["id"]])[ko["id"]]
    assert usage["positive_feedback_count"] == 0
    assert usage["negative_feedback_count"] == 1


@pytest.mark.asyncio
async def test_repeated_rejections_only_downgrade_future_automatic_promotion(settings, storage):
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    sample = "Сервер Atlas работает на Ubuntu 24.04."
    baseline = pipeline.assess_text(sample)
    assert baseline.action == "promote"
    for index in range(3):
        storage.store_feedback(
            FeedbackItem(
                id=new_id("feedback"),
                user_id="alice",
                target_type="classification",
                target_id=f"raw-rejected-{index}",
                feedback_type=FeedbackType.CLASSIFICATION,
                score=-1.0,
                context_json={
                    "knowledge_kind": baseline.knowledge_kind,
                    "signals": baseline.signals,
                    "status": "ignored",
                },
            )
        )

    calibrated = await pipeline.ingest_text(
        "alice",
        "Сервер Borealis работает на Ubuntu 24.04.",
        source_ref="calibration:auto",
    )
    assert calibrated["action"] == "review"
    assert calibrated["queued_for_review"] is True
    assert (
        "feedback_calibration_review"
        in (storage.get_raw_object(calibrated["raw_object_id"], "alice")["metadata_json"])
    )

    explicit = await pipeline.ingest_text(
        "alice",
        "Запомни: сервер Borealis работает на Ubuntu 24.04.",
        source_ref="calibration:explicit",
    )
    assert explicit["action"] == "promote"
    no_save = await pipeline.ingest_text(
        "alice",
        "Не запоминай: сервер Borealis работает на Ubuntu 24.04.",
        source_ref="calibration:no-save",
        force_knowledge=True,
    )
    assert no_save["action"] == "transient"
    assert no_save["raw_object_id"] is None


@pytest.mark.asyncio
async def test_research_synthesis_crosses_only_the_inbox_boundary(settings, storage):
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    result = await pipeline.queue_research_candidate(
        "alice",
        "Исследование: PostgreSQL 16 улучшает logical replication; перед применением сверить источники.",
        source_ref="research-answer:msg-1",
        metadata={"assistant_message_id": "msg-1"},
    )
    assert result["promoted"] is False
    assert result["queued_for_review"] is True
    inbox = storage.get_inbox_item(result["inbox_id"], "alice")
    assert inbox and inbox["status"] == InboxStatus.PENDING.value
    assert storage.get_knowledge_by_raw(result["raw_object_id"], "alice") is None

    replay = await pipeline.queue_research_candidate(
        "alice",
        "Исследование: PostgreSQL 16 улучшает logical replication; перед применением сверить источники.",
        source_ref="research-answer:msg-1",
    )
    assert replay["idempotent_replay"] is True
    assert replay["inbox_id"] == result["inbox_id"]


def test_conversation_modes_and_lifecycle_candidates_are_persistent_and_safe(storage):
    assert SCHEMA_VERSION == 18
    conversation = storage.create_conversation("alice", "Research", mode="research")
    assert conversation["mode"] == "research"
    storage.set_channel_conversation(
        "alice",
        "telegram",
        "42",
        conversation["id"],
        mode="research",
    )
    assert storage.get_channel_session("alice", "telegram", "42")["mode"] == "research"
    assert storage.set_channel_mode("alice", "telegram", "42", "knowledge_work")["mode"] == "knowledge_work"

    stale = _store_knowledge(storage, "alice", "Old low-value note", title="Old note")
    reviewed = _store_knowledge(
        storage,
        "alice",
        "Manually reviewed old note",
        title="Reviewed note",
        metadata={"promotion_assessment": {"reason": "human review"}},
    )
    old_timestamp = (datetime.now(UTC) - timedelta(days=400)).isoformat(timespec="seconds")
    storage.execute(
        "UPDATE knowledge_objects SET updated_at=? WHERE id IN (?, ?)",
        (old_timestamp, stale["id"], reviewed["id"]),
    )
    candidates = storage.list_lifecycle_candidates("alice", days_threshold=90)
    assert [item["knowledge_object"]["id"] for item in candidates] == [stale["id"]]
    assert storage.get_knowledge_object(stale["id"], "alice")["lifecycle_stage"] == "active"

    storage.record_knowledge_usage("alice", [stale["id"]], used_in_answer=True)
    assert storage.list_lifecycle_candidates("alice", days_threshold=90) == []


@pytest.mark.asyncio
async def test_bounded_local_vision_creates_advisory_inbox_item(settings, storage):
    class FakeVisionLLM:
        enabled = True
        model = "fake-qwen-vision"

        async def chat(self, messages, **kwargs):
            assert kwargs["temperature"] == 0.0
            assert kwargs["priority"] == "foreground"
            user_content = messages[-1]["content"]
            assert isinstance(user_content, list)
            assert any(item.get("type") == "image_url" for item in user_content)
            assert any("ASSET A1" in str(item.get("text") or "") for item in user_content)
            return {
                "content": json.dumps(
                    {
                        "text": "Схема проекта Orion: PostgreSQL 16",
                        "title": "Схема Orion",
                        "summary": "На изображении указана база PostgreSQL 16 проекта Orion.",
                        "entities": [
                            {
                                "name": "Orion",
                                "entity_type": "project",
                                "confidence": 0.98,
                                "asset_id": "A1",
                                "evidence": "проект Orion",
                            },
                            {
                                "name": "PostgreSQL",
                                "entity_type": "concept",
                                "confidence": 0.97,
                                "asset_id": "A1",
                                "evidence": "PostgreSQL 16",
                            },
                        ],
                        "evidence": [
                            {
                                "asset_id": "A1",
                                "quote": "проект Orion: PostgreSQL 16",
                                "claim": "Orion использует PostgreSQL 16",
                            }
                        ],
                        "warnings": [],
                        "confidence": 0.87,
                    },
                    ensure_ascii=False,
                )
            }

    image = Image.new("RGB", (640, 360), "white")
    data = BytesIO()
    image.save(data, format="PNG")
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), FakeVisionLLM())
    result = await pipeline.ingest_file(
        "alice",
        None,
        data.getvalue(),
        filename="orion-scan.png",
        mime_type="image/png",
        source_ref="vision:orion",
    )
    # Review-gated invariant (§24): vision output is model-generated, so no
    # Knowledge Object exists until a human confirms the pending inbox item.
    assert result["promoted"] is False
    assert result["queued_for_review"] is True
    assert result["knowledge_object"] is None
    assert storage.get_knowledge_by_raw(result["raw_object_id"], "alice") is None
    assert result["extraction"]["vision"]["confidence"] == 0.87
    assert result["extraction"]["vision"]["grounded_evidence_count"] == 1
    assert result["extraction"]["vision"]["asset_coverage"] == 1.0
    inbox = storage.find_inbox_by_raw(result["raw_object_id"], "alice")
    assert inbox and inbox["status"] == "pending"
    assert inbox["knowledge_object_id"] is None
    # Uncertain visual entities remain Inbox suggestions instead of polluting
    # the graph with new nodes; the caps survive into deferred promotion.
    suggestions = json.loads(inbox["suggestions_json"])
    assert {item["name"] for item in suggestions["entities"]} >= {"Orion", "PostgreSQL"}
    assert all(float(item["confidence"]) <= 0.79 for item in suggestions["entities"])


def test_current_feedback_stats_replace_superseded_signal(storage):
    storage.ensure_user("alice")
    for score in (1.0, -1.0):
        storage.store_feedback(
            FeedbackItem(
                id=new_id("fb"),
                user_id="alice",
                target_type="answer",
                target_id="answer-1",
                feedback_type=FeedbackType.ANSWER_USEFULNESS,
                score=score,
            )
        )

    history = storage.get_feedback_stats("alice", target_type="answer")
    current = storage.get_current_feedback_stats("alice", target_type="answer")
    key = FeedbackType.ANSWER_USEFULNESS.value
    assert history[key]["count"] == 2
    assert current[key] == {"avg_score": -1.0, "count": 1, "positive": 0, "negative": 1}


@pytest.mark.asyncio
async def test_agent_feedback_attribution_uses_only_cited_knowledge(settings, storage):
    from jericho.agent_runtime import AgentRuntime
    from jericho.permissions import ActorContext

    first = _store_knowledge(storage, "alice", "Atlas uses Redis.", title="Atlas cache")
    second = _store_knowledge(storage, "alice", "Atlas uses PostgreSQL 16.", title="Atlas database")

    class FakeSearcher:
        async def search(self, user_id, query, **kwargs):
            assert user_id == "alice"
            assert query
            assert kwargs["limit"] == 16
            return {
                "results": [
                    {**first, "_score": 0.91, "_entities": []},
                    {**second, "_score": 0.88, "_entities": []},
                ],
                "entity_matches": [],
            }

    class CitationLLM:
        enabled = True
        model = "citation-test"

        async def chat(self, messages, **kwargs):
            del kwargs
            context_message = next(
                item["content"]
                for item in messages
                if item.get("role") == "user"
                and str(item.get("content") or "").startswith("JERICHO_CONTEXT_DATA")
            )
            assert '"citation": "K1"' in context_message
            assert '"citation": "K2"' in context_message
            return {"content": "Для рабочей базы Atlas используется PostgreSQL 16 [K2]."}

    runtime = AgentRuntime(settings, storage, llm=CitationLLM())
    response = await runtime.chat(
        "alice",
        "Подготовь структурированную справку по базе Atlas",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        enable_tools=False,
        hybrid_searcher=FakeSearcher(),
        mode="knowledge_work",
    )

    message = storage.get_message(response["message_id"], "alice")
    assert message is not None
    metadata = json.loads(message["metadata_json"])
    assert metadata["knowledge_object_ids"] == [second["id"]]
    assert metadata["knowledge_citations"] == {"K2": second["id"]}
    usage = storage.get_knowledge_usage("alice", [first["id"], second["id"]])
    assert first["id"] not in usage
    assert usage[second["id"]]["answer_count"] == 1

    await runtime.record_feedback(
        "alice",
        "answer",
        response["message_id"],
        FeedbackType.ANSWER_USEFULNESS,
        -1.0,
        context={
            "channel": "api",
            "knowledge_object_ids": [first["id"]],
            "interaction_mode": "research",
        },
    )
    state = storage.get_feedback_state(
        "alice",
        target_type="answer",
        target_id=response["message_id"],
        feedback_type=FeedbackType.ANSWER_USEFULNESS.value,
    )[0]
    feedback_context = json.loads(state["context_json"])
    assert feedback_context["knowledge_object_ids"] == [second["id"]]
    assert feedback_context["knowledge_citations"] == {"K2": second["id"]}
    assert feedback_context["interaction_mode"] == "knowledge_work"
    usage = storage.get_knowledge_usage("alice", [first["id"], second["id"]])
    assert first["id"] not in usage
    assert usage[second["id"]]["negative_feedback_count"] == 1

    with pytest.raises(LookupError, match="Assistant answer not found"):
        await runtime.record_feedback(
            "alice",
            "answer",
            "missing-message",
            FeedbackType.ANSWER_USEFULNESS,
            1.0,
        )


@pytest.mark.asyncio
async def test_ungrounded_vision_confidence_is_capped_and_remains_advisory(settings, storage):
    class UngroundedVisionLLM:
        enabled = True
        model = "ungrounded-vision-test"

        async def chat(self, messages, **kwargs):
            del messages, kwargs
            return {
                "content": json.dumps(
                    {
                        "text": "Проект Aurora использует неизвестную БД.",
                        "title": "Скан Aurora",
                        "summary": "Якобы распознана архитектурная схема.",
                        "document_type": "diagram",
                        "entities": [
                            {
                                "name": "Aurora",
                                "entity_type": "project",
                                "confidence": 0.99,
                            }
                        ],
                        "evidence": [],
                        "warnings": [],
                        "confidence": 0.99,
                    },
                    ensure_ascii=False,
                )
            }

    image = Image.new("RGB", (640, 360), "white")
    data = BytesIO()
    image.save(data, format="PNG")
    pipeline = IngestionPipeline(
        settings,
        storage,
        KnowledgeGraph(storage),
        UngroundedVisionLLM(),
    )
    result = await pipeline.ingest_file(
        "alice",
        None,
        data.getvalue(),
        filename="aurora-ungrounded.png",
        mime_type="image/png",
        source_ref="vision:aurora:ungrounded",
    )

    vision = result["extraction"]["vision"]
    assert vision["confidence"] <= 0.55
    assert vision["grounded_evidence_count"] == 0
    assert vision["warnings"]
    inbox = storage.find_inbox_by_raw(result["raw_object_id"], "alice")
    assert inbox is not None and inbox["status"] == "pending"
    suggestions = json.loads(inbox["suggestions_json"])
    assert all(float(item["confidence"]) <= 0.55 for item in suggestions["entities"])
