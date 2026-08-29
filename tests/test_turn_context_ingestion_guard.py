from __future__ import annotations

import hashlib
import json
import time

import pytest

from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.orchestration.contracts import RouterMode
from friday.orchestration.turn_context import IngressKind, TurnContextError, TurnContextIssuer, TurnMode
from friday.orchestration.turn_context_ingress import issue_authenticated_scalar_turn_context
from friday.orchestration.turn_context_runtime import bind_authenticated_turn_context
from friday.permissions import ActorContext
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision

_KEY = "authenticated_turn_ingestion"
_CONVERSATION_ID = "conv_0123456789abcdef"


def _turn(now: list[int], *, label: str, user_id: str = "owner"):
    issuer = TurnContextIssuer(
        hashlib.sha256(f"authenticated-ingestion-test:{label}".encode("ascii")).digest(),
        _monotonic_ns=lambda: now[0],
    )
    actor = ActorContext(user_id=user_id, preset_key="owner", source="api-token")
    context = issue_authenticated_scalar_turn_context(
        issuer,
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=f"ingestion-source:{label}",
        actor=actor,
        conversation_id=_CONVERSATION_ID,
        interaction_mode=TurnMode.DIALOGUE,
        source_id=actor.source,
        update_id=f"ingestion-source:{label}",
        request_effect_binding_sha256=hashlib.sha256(
            f"ingestion-effects:{label}".encode("ascii")
        ).hexdigest(),
        message="Запомни проверяемый синтетический факт.",
        enable_tools=True,
        decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
        router_mode=RouterMode.LEGACY,
        deadline_monotonic_ns=now[0] + 10_000_000_000,
        max_output_tokens=2048,
    )
    return issuer, context


@pytest.mark.asyncio
async def test_exact_ingestion_persists_only_a_body_free_turn_projection(settings, storage) -> None:
    now = [time.monotonic_ns()]
    issuer, context = _turn(now, label="exact")
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)
    caller_metadata = {"channel": "api-token"}

    with bind_authenticated_turn_context(issuer, context):
        outcome = await pipeline.ingest_text(
            "owner",
            context.model_input.message,
            source="api",
            source_ref=context.authority.update_id,
            force_knowledge=True,
            metadata=caller_metadata,
            _authenticated_turn_context=context,
        )

    raw = storage.get_raw_object(str(outcome["raw_object_id"]), "owner")
    metadata = json.loads(str(raw["metadata_json"]))
    assert caller_metadata == {"channel": "api-token"}
    assert metadata[_KEY] == {
        "schema": "friday.authenticated-turn-ingestion.v1",
        "turn_id": context.turn_id,
        "context_authority_sha256": context.context_authority_sha256,
        "request_effect_binding_sha256": context.effect_fence.request_effect_binding_sha256,
        "relation": "accepted_ingress",
    }
    serialized = json.dumps(metadata[_KEY], ensure_ascii=True)
    assert context.model_input.message not in serialized


@pytest.mark.asyncio
async def test_derived_ingestion_keeps_the_same_turn_without_relabeling_the_ingress(
    settings,
    storage,
) -> None:
    now = [time.monotonic_ns()]
    issuer, context = _turn(now, label="derived")
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)

    with bind_authenticated_turn_context(issuer, context):
        outcome = await pipeline.ingest_text(
            "owner",
            "Публичная производная страница достаточной длины. " * 8,
            source="web",
            source_ref="https://example.test/page#digest",
            force_review=True,
        )

    raw = storage.get_raw_object(str(outcome["raw_object_id"]), "owner")
    metadata = json.loads(str(raw["metadata_json"]))
    assert metadata[_KEY]["turn_id"] == context.turn_id
    assert metadata[_KEY]["relation"] == "derived_effect"


@pytest.mark.asyncio
async def test_ingestion_drift_and_reserved_spoof_fail_before_raw_persistence(settings, storage) -> None:
    now = [time.monotonic_ns()]
    issuer, context = _turn(now, label="drift")
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)

    with bind_authenticated_turn_context(issuer, context):
        with pytest.raises(TurnContextError, match="tenant drifted"):
            await pipeline.ingest_text(
                "another-tenant",
                context.model_input.message,
                source="api",
                source_ref="tenant-drift",
                _authenticated_turn_context=context,
            )
        with pytest.raises(TurnContextError, match="source identity drifted"):
            await pipeline.ingest_text(
                "owner",
                context.model_input.message,
                source="api",
                source_ref="wrong-ingress-source",
                _authenticated_turn_context=context,
            )
        with pytest.raises(TurnContextError, match="metadata is reserved"):
            await pipeline.ingest_text(
                "owner",
                context.model_input.message,
                source="api",
                source_ref="metadata-spoof",
                metadata={_KEY: {"turn_id": "forged"}},
                _authenticated_turn_context=context,
            )

    assert storage.find_raw_by_source_ref("another-tenant", "api", "tenant-drift") is None
    assert storage.find_raw_by_source_ref("owner", "api", "wrong-ingress-source") is None
    assert storage.find_raw_by_source_ref("owner", "api", "metadata-spoof") is None


@pytest.mark.asyncio
async def test_context_expiry_during_enrichment_prevents_raw_commit(
    settings,
    storage,
    monkeypatch,
) -> None:
    now = [time.monotonic_ns()]
    issuer, context = _turn(now, label="expiry")
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)
    original_enrich = pipeline._enrich  # noqa: SLF001

    def expire_then_enrich(*args, **kwargs):
        result = original_enrich(*args, **kwargs)
        now[0] = context.inherited_budget.safety_deadline.monotonic_ns
        return result

    monkeypatch.setattr(pipeline, "_enrich", expire_then_enrich)
    with bind_authenticated_turn_context(issuer, context), pytest.raises(TurnContextError, match="deadline"):
        await pipeline.ingest_text(
            "owner",
            context.model_input.message,
            source="api",
            source_ref=context.authority.update_id,
            force_knowledge=True,
            _authenticated_turn_context=context,
        )

    assert storage.find_raw_by_source_ref("owner", "api", context.authority.update_id) is None


@pytest.mark.asyncio
async def test_context_expiry_after_first_raw_write_rolls_back_the_complete_unit(
    settings,
    storage,
    monkeypatch,
) -> None:
    now = [time.monotonic_ns()]
    issuer, context = _turn(now, label="commit-boundary")
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)
    original_store_raw_object = storage.store_raw_object

    def store_then_expire(raw):
        stored = original_store_raw_object(raw)
        now[0] = context.inherited_budget.safety_deadline.monotonic_ns
        return stored

    monkeypatch.setattr(storage, "store_raw_object", store_then_expire)
    with (
        bind_authenticated_turn_context(issuer, context),
        pytest.raises(
            TurnContextError,
            match="deadline",
        ),
    ):
        await pipeline.ingest_text(
            "owner",
            context.model_input.message,
            source="api",
            source_ref=context.authority.update_id,
            force_knowledge=True,
            _authenticated_turn_context=context,
        )

    assert storage.find_raw_by_source_ref("owner", "api", context.authority.update_id) is None


@pytest.mark.asyncio
async def test_legacy_metadata_bytes_remain_unchanged_and_reserved_key_is_closed(
    settings,
    storage,
) -> None:
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)
    outcome = await pipeline.ingest_text(
        "legacy",
        "Запомни обычный legacy-факт.",
        source="api",
        source_ref="legacy-ingestion",
        force_knowledge=True,
        metadata={"legacy": {"ordinal": 1}},
    )
    raw = storage.get_raw_object(str(outcome["raw_object_id"]), "legacy")
    metadata = json.loads(str(raw["metadata_json"]))
    assert metadata["legacy"] == {"ordinal": 1}
    assert _KEY not in metadata

    with pytest.raises(TurnContextError, match="metadata is reserved"):
        await pipeline.ingest_text(
            "legacy",
            "Запомни второй legacy-факт.",
            source="api",
            source_ref="legacy-spoof",
            force_knowledge=True,
            metadata={_KEY: {}},
        )
    assert storage.find_raw_by_source_ref("legacy", "api", "legacy-spoof") is None
