from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from types import MappingProxyType
from typing import Any

import httpx
import pytest

import friday.secondary_brain.profiles as secondary_profiles
from friday.ingestion import IngestionPipeline
from friday.ingestion._secondary_advice import route_inbox_advice, valid_inbox_advice_shape
from friday.knowledge_graph import KnowledgeGraph
from friday.secondary_brain import SecondaryState, build_secondary_brain
from friday.secondary_brain.profiles import SecondaryRuntimeProfile

_API_KEY = "b" * 64
_ENGINE_PROJECTION: dict[str, Any] = {
    "source_model_repository": "openai/gpt-oss-20b",
    "source_model_revision": "6cee5e81ee83917806bbde320786a8fb61efebee",
    "hardware_runtime_receipt_sha256": "0c1c9e6f54aa0004c3dfc89acd6904cfbb0f834d0988e971e34b9699b3d9031f",
    "source_model_manifest_sha256": "438df0a0b2f6b4164c2fd9d9ed309925abbc94ed8deb056b692d2ccad7887fd9",
    "runtime_image": "lmsysorg/sglang@sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405",
    "runtime_image_config_digest": "sha256:f7adc6c05df9ff711b82ad291cf1db6eaf30590c4d929833d632abfef3895efc",
    "runtime_image_oci_manifest_digest": "sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405",
    "runtime_source_revision": "29481685462732237d80d86076d6563e1f658102",
    "sglang_compat_patch_sha256": "d" * 64,
    "runtime_manifest_sha256": "e" * 64,
    "model_path": "/source/snapshot",
    "quantization": "mxfp4",
    "dtype": "bfloat16",
    "kv_cache_dtype": "bf16",
    "kv_cache_scale_policy": "not_applicable",
    "attention_backend": "triton",
    "prefill_attention_backend": "triton",
    "decode_attention_backend": "triton",
    "sampling_backend": "pytorch",
    "moe_runner_backend": "flashinfer_mxfp4",
    "mxfp4_moe_precision": "default",
    "mm_feature_transport": "cpu",
    "context_tokens": 16_384,
    "max_total_tokens": 16_384,
    "mem_fraction_static": "0.97",
    "max_running_requests": 1,
    "max_output_tokens": 2048,
    "chunked_prefill_size": 1024,
    "page_size": 1,
    "radix_cache_enabled": True,
    "overlap_schedule_enabled": True,
    "hybrid_swa_memory_enabled": True,
    "swa_full_tokens_ratio": "0.80",
    "cuda_graph_backend_decode": "disabled",
    "cuda_graph_backend_prefill": "disabled",
    "cuda_graph_max_bs_decode": 0,
    "cuda_graph_bs_decode": [],
    "no_cpu_offload": True,
}
_ENGINE_BINDING_SHA256 = hashlib.sha256(
    (json.dumps(_ENGINE_PROJECTION, sort_keys=True, separators=(",", ":")) + "\n").encode()
).hexdigest()
_PROFILE_ID = f"gptoss20b-{_ENGINE_BINDING_SHA256}"
_ALIAS = f"friday-secondary-{_PROFILE_ID}"
_PROFILE_VALUE: dict[str, Any] = {
    **_ENGINE_PROJECTION,
    "schema": "friday.secondary-runtime-profile.v4",
    "status": "accepted",
    "profile_id": _PROFILE_ID,
    "engine_binding_sha256": _ENGINE_BINDING_SHA256,
    "endpoint_base_url": "http://127.0.0.1:30001/v1",
    "served_model_alias": _ALIAS,
    "gateway_ca_certificate_sha256": "",
    "allowed_modes": ["assist", "shadow"],
    "allowed_workloads": ["extract"],
    "quality_evidence_sha256": "6" * 64,
    "capacity_evidence_sha256": "7" * 64,
    "soak_evidence_sha256": "8" * 64,
    "failure_evidence_sha256": "9" * 64,
}
_PROFILE_BYTES = (
    json.dumps(_PROFILE_VALUE, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
).encode("utf-8")
_PROFILE_SHA256 = hashlib.sha256(_PROFILE_BYTES).hexdigest()
_PROFILE_HEADERS = {
    "X-Friday-Secondary-Profile-Id": _PROFILE_ID,
    "X-Friday-Secondary-Profile-Sha256": _PROFILE_SHA256,
}


@pytest.fixture(autouse=True)
def _accepted_test_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = SecondaryRuntimeProfile(
        profile_id=_PROFILE_ID,
        endpoint_base_url="http://127.0.0.1:30001/v1",
        served_model_alias=_ALIAS,
        manifest_sha256=_PROFILE_SHA256,
        engine_binding_sha256=_ENGINE_BINDING_SHA256,
        hardware_runtime_receipt_sha256=_ENGINE_PROJECTION["hardware_runtime_receipt_sha256"],
        gateway_ca_certificate_sha256="",
        max_context_tokens=16_384,
        max_total_tokens=16_384,
        max_concurrency=1,
        max_output_tokens=2048,
        chunked_prefill_size=1024,
        mem_fraction_static="0.97",
        quantization="mxfp4",
        dtype="bfloat16",
        kv_cache_dtype="bf16",
        kv_cache_scale_policy="not_applicable",
        attention_backend="triton",
        prefill_attention_backend="triton",
        decode_attention_backend="triton",
        sampling_backend="pytorch",
        moe_runner_backend="flashinfer_mxfp4",
        mxfp4_moe_precision="default",
        mm_feature_transport="cpu",
        page_size=1,
        radix_cache_enabled=True,
        overlap_schedule_enabled=True,
        hybrid_swa_memory_enabled=True,
        swa_full_tokens_ratio="0.80",
        cuda_graph_backend_decode="disabled",
        cuda_graph_backend_prefill="disabled",
        cuda_graph_max_bs_decode=0,
        cuda_graph_bs_decode=(),
        no_cpu_offload=True,
        allowed_modes=frozenset({"shadow", "assist"}),
        allowed_workloads=frozenset({"extract"}),
        model_repository="openai/gpt-oss-20b",
        model_revision="6cee5e81ee83917806bbde320786a8fb61efebee",
        source_model_manifest_sha256=_ENGINE_PROJECTION["source_model_manifest_sha256"],
        model_path="/source/snapshot",
        runtime_image=_ENGINE_PROJECTION["runtime_image"],
        runtime_image_config_digest=_ENGINE_PROJECTION["runtime_image_config_digest"],
        runtime_image_oci_manifest_digest=_ENGINE_PROJECTION["runtime_image_oci_manifest_digest"],
        runtime_source_revision=_ENGINE_PROJECTION["runtime_source_revision"],
        sglang_compat_patch_sha256=_ENGINE_PROJECTION["sglang_compat_patch_sha256"],
        runtime_manifest_sha256="e" * 64,
    )
    monkeypatch.setattr(
        secondary_profiles,
        "ACCEPTED_SECONDARY_RUNTIME_PROFILES",
        MappingProxyType({_PROFILE_ID: profile}),
    )


def _advice(title: str) -> dict[str, Any]:
    return {
        "title": title,
        "summary": "Рассмотреть Redis как кеш для сервера.",
        "knowledge_kind": "technical_note",
        "importance": 0.61,
        "tags": ["redis", "кеш"],
        "entities": [
            {
                "name": "Redis",
                "entity_type": "concept",
                "confidence": 0.78,
                "evidence": "Redis буквально указан в исходнике",
            }
        ],
        "recommended_action": "review",
        "confidence": 0.82,
        "rationale": "Техническая идея требует решения владельца.",
    }


def _completion(payload: dict[str, Any]) -> httpx.Response:
    return _completion_text(json.dumps(payload, ensure_ascii=False))


def _completion_text(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers=_PROFILE_HEADERS,
        json={
            "model": _ALIAS,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 80, "total_tokens": 180},
        },
    )


def _secondary_settings(
    settings: Any,
    *,
    mode: str = "assist",
    allow_private: bool = True,
    call_budget: float = 0.2,
) -> Any:
    return replace(
        settings,
        secondary_llm_enabled=True,
        secondary_llm_mode=mode,
        secondary_llm_base_url="http://127.0.0.1:30001/v1",
        secondary_llm_model=_ALIAS,
        secondary_llm_api_key=_API_KEY,
        secondary_llm_connect_timeout_sec=min(0.01, call_budget),
        secondary_llm_call_budget_sec=call_budget,
        secondary_llm_read_timeout_sec=call_budget,
        secondary_llm_max_context_tokens=16_384,
        secondary_llm_profile=_PROFILE_ID,
        secondary_llm_workloads=("extract",),
        secondary_llm_allow_private_text=allow_private,
    )


def _after_admission(handler: Any) -> httpx.MockTransport:
    async def admitted(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/friday-profile"):
            return httpx.Response(200, headers=_PROFILE_HEADERS, content=_PROFILE_BYTES)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, headers=_PROFILE_HEADERS, json={"data": [{"id": _ALIAS}]})
        payload = json.loads(request.content)
        assert payload["reasoning_effort"] == "low"
        assert payload["temperature"] == 1.0
        assert payload["top_p"] == 1.0
        assert payload["seed"] == 0
        messages = payload.get("messages", [])
        if messages and messages[-1].get("content") == "Reply with exactly: ready":
            return httpx.Response(
                200,
                headers=_PROFILE_HEADERS,
                json={
                    "model": _ALIAS,
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "ready"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )
        return await handler(request)

    return httpx.MockTransport(admitted)


class _Primary:
    enabled = True
    model = "primary-model"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0
        self.requests: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        self.requests.append((messages, kwargs))
        return {"content": json.dumps(self.payload, ensure_ascii=False)}


async def _pending_item(pipeline: IngestionPipeline, source_ref: str) -> str:
    ingested = await pipeline.ingest_text(
        "alice",
        "Идея: когда-нибудь добавить Redis для кеша сервера",
        source_ref=source_ref,
    )
    assert ingested["action"] == "review"
    return str(ingested["inbox_id"])


@pytest.mark.asyncio
async def test_assist_uses_validated_secondary_extraction_without_primary(
    settings: Any, storage: Any
) -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        body = json.loads(request.content)
        assert body["model"] == _ALIAS
        assert "tools" not in body
        return _completion(_advice("Secondary Redis advice"))

    scheduler = build_secondary_brain(
        _secondary_settings(settings),
        transport=_after_admission(handler),
    )
    primary: Any = _Primary(_advice("Primary Redis advice"))
    pipeline = IngestionPipeline(
        settings,
        storage,
        KnowledgeGraph(storage),
        primary,
        secondary_brain=scheduler,
    )
    try:
        inbox_id = await _pending_item(pipeline, "secondary-advice:assist")
        result = await pipeline.advise_inbox_item("alice", inbox_id, llm=primary)

        assert result["suggestions"]["title"] == "Secondary Redis advice"
        assert result["model_advice"]["model"] == _ALIAS
        assert result["model_advice"]["endpoint_role"] == "secondary"
        assert primary.calls == 0
        assert requests == 1
        assert scheduler.status().state is SecondaryState.HEALTHY
        assert storage.count_knowledge_objects("alice") == 0
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["refusal", "timeout"])
async def test_laptop_failure_preserves_primary_advice_once(
    settings: Any, storage: Any, failure: str
) -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if failure == "timeout":
            await asyncio.sleep(1.0)
            raise AssertionError("secondary timeout was not cancelled")
        raise httpx.ConnectError("synthetic refusal", request=request)

    scheduler = build_secondary_brain(
        _secondary_settings(settings, call_budget=0.02),
        transport=httpx.MockTransport(handler),
    )
    primary_payload = _advice("Exact primary fallback")
    primary: Any = _Primary(primary_payload)
    pipeline = IngestionPipeline(
        settings,
        storage,
        KnowledgeGraph(storage),
        primary,
        secondary_brain=scheduler,
    )
    try:
        inbox_id = await _pending_item(pipeline, f"secondary-advice:{failure}")
        result = await pipeline.advise_inbox_item("alice", inbox_id, llm=primary)

        assert result["suggestions"]["title"] == "Exact primary fallback"
        assert result["model_advice"]["model"] == primary.model
        assert result["model_advice"]["endpoint_role"] == "primary"
        assert primary.calls == 1
        assert requests == 1
        sent_messages, sent_kwargs = primary.requests[0]
        assert "Исходный материал" in sent_messages[1]["content"]
        assert sent_kwargs == {
            "temperature": 0.0,
            "max_tokens": settings.cognition_max_tokens,
            "priority": "background",
            "tools": [],
        }
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_invalid_secondary_schema_falls_back_to_primary_once(settings: Any, storage: Any) -> None:
    invalid = _advice("Invalid secondary")
    invalid.pop("recommended_action")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return _completion(invalid)

    scheduler = build_secondary_brain(
        _secondary_settings(settings),
        transport=_after_admission(handler),
    )
    primary: Any = _Primary(_advice("Validated primary"))
    pipeline = IngestionPipeline(
        settings,
        storage,
        KnowledgeGraph(storage),
        primary,
        secondary_brain=scheduler,
    )
    try:
        inbox_id = await _pending_item(pipeline, "secondary-advice:invalid")
        result = await pipeline.advise_inbox_item("alice", inbox_id, llm=primary)

        assert result["suggestions"]["title"] == "Validated primary"
        assert primary.calls == 1
        diagnostics = scheduler.diagnostics_status()
        assert diagnostics["primary_fallback_total"] == 1
        assert diagnostics["fallback_reasons"] == {"malformed_response": 1}
        assert diagnostics["workloads"]["extract"]["success_total"] == 0
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_valid_padded_secondary_json_is_compacted_before_downstream_parse(
    settings: Any,
    storage: Any,
) -> None:
    payload = _advice("Compact validated advice")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return _completion_text(" " * 70_000 + json.dumps(payload, ensure_ascii=False))

    scheduler = build_secondary_brain(
        _secondary_settings(settings),
        transport=_after_admission(handler),
    )
    primary: Any = _Primary(_advice("must not fall back"))
    pipeline = IngestionPipeline(
        settings,
        storage,
        KnowledgeGraph(storage),
        primary,
        secondary_brain=scheduler,
    )
    try:
        inbox_id = await _pending_item(pipeline, "secondary-advice:padded")
        result = await pipeline.advise_inbox_item("alice", inbox_id, llm=primary)
        assert result["suggestions"]["title"] == "Compact validated advice"
        assert result["model_advice"]["endpoint_role"] == "secondary"
        assert primary.calls == 0
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["huge_number", "surrogate"])
async def test_non_total_json_values_fall_back_once_without_worker_failure(
    settings: Any,
    storage: Any,
    invalid_kind: str,
) -> None:
    payload = _advice("Invalid typed value")
    if invalid_kind == "huge_number":
        payload["importance"] = 10**400
        content = json.dumps(payload, ensure_ascii=False)
    else:
        payload["title"] = "\ud800"
        content = json.dumps(payload, ensure_ascii=True)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return _completion_text(content)

    scheduler = build_secondary_brain(
        _secondary_settings(settings),
        transport=_after_admission(handler),
    )
    primary: Any = _Primary(_advice("Safe primary fallback"))
    pipeline = IngestionPipeline(
        settings,
        storage,
        KnowledgeGraph(storage),
        primary,
        secondary_brain=scheduler,
    )
    try:
        inbox_id = await _pending_item(pipeline, f"secondary-advice:{invalid_kind}")
        result = await pipeline.advise_inbox_item("alice", inbox_id, llm=primary)
        assert result["suggestions"]["title"] == "Safe primary fallback"
        assert primary.calls == 1
    finally:
        await scheduler.aclose()


def test_semantic_validator_is_total_for_unbounded_numbers() -> None:
    payload = _advice("Huge score")
    payload["importance"] = 10**400
    assert valid_inbox_advice_shape(payload) is False


@pytest.mark.asyncio
async def test_shadow_keeps_exact_primary_result_and_discards_secondary(settings: Any, storage: Any) -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _completion(_advice("Discarded shadow"))

    scheduler = build_secondary_brain(
        _secondary_settings(settings, mode="shadow"),
        transport=_after_admission(handler),
    )
    primary: Any = _Primary(_advice("Primary remains authoritative"))
    pipeline = IngestionPipeline(
        settings,
        storage,
        KnowledgeGraph(storage),
        primary,
        secondary_brain=scheduler,
    )
    try:
        inbox_id = await _pending_item(pipeline, "secondary-advice:shadow")
        result = await pipeline.advise_inbox_item("alice", inbox_id, llm=primary)

        assert result["suggestions"]["title"] == "Primary remains authoritative"
        assert result["model_advice"]["model"] == primary.model
        assert primary.calls == 1
        await scheduler.drain_shadow()
        assert requests == 1
        assert scheduler.diagnostics_status()["shadow"] == {
            "valid_total": 1,
            "invalid_total": 0,
            "skipped_total": 0,
            "in_flight": 0,
        }
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_shadow_deadline_starts_after_slow_primary_finishes(settings: Any, storage: Any) -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _completion(_advice("Fresh shadow"))

    class SlowPrimary(_Primary):
        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(0.25)
            return await super().chat(messages, **kwargs)

    scheduler = build_secondary_brain(
        _secondary_settings(settings, mode="shadow", call_budget=0.2),
        transport=_after_admission(handler),
    )
    primary: Any = SlowPrimary(_advice("Slow primary"))
    pipeline = IngestionPipeline(
        settings,
        storage,
        KnowledgeGraph(storage),
        primary,
        secondary_brain=scheduler,
    )
    try:
        inbox_id = await _pending_item(pipeline, "secondary-advice:fresh-shadow-deadline")
        result = await pipeline.advise_inbox_item("alice", inbox_id, llm=primary)
        assert result["suggestions"]["title"] == "Slow primary"
        await scheduler.drain_shadow()
        assert requests == 1
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_disabled_route_returns_the_exact_primary_object(settings: Any) -> None:
    scheduler = build_secondary_brain(settings)
    primary_response = {"content": json.dumps(_advice("Primary object"), ensure_ascii=False)}
    primary_calls = 0

    async def primary() -> dict[str, Any]:
        nonlocal primary_calls
        primary_calls += 1
        return primary_response

    routed = await route_inbox_advice(
        secondary=scheduler,
        messages=({"role": "user", "content": "private source"},),
        max_output_tokens=64,
        primary_model_name="primary-model",
        primary_call=primary,
        contains_private_text=True,
        image_bearing=False,
    )

    assert routed.response is primary_response
    assert routed.source == "primary"
    assert primary_calls == 1
    assert scheduler.served_model_alias == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allow_private", "image_bearing"),
    [(False, False), (True, True)],
)
async def test_private_or_image_material_never_reaches_secondary(
    settings: Any, allow_private: bool, image_bearing: bool
) -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _completion(_advice("must not be called"))

    scheduler = build_secondary_brain(
        _secondary_settings(settings, allow_private=allow_private),
        transport=httpx.MockTransport(handler),
    )
    primary_response = {"content": json.dumps(_advice("Private primary"), ensure_ascii=False)}
    primary_calls = 0

    async def primary() -> dict[str, Any]:
        nonlocal primary_calls
        primary_calls += 1
        return primary_response

    try:
        routed = await route_inbox_advice(
            secondary=scheduler,
            messages=({"role": "user", "content": "private source"},),
            max_output_tokens=64,
            primary_model_name="primary-model",
            primary_call=primary,
            contains_private_text=True,
            image_bearing=image_bearing,
        )

        assert routed.response is primary_response
        assert primary_calls == 1
        assert requests == 0
    finally:
        await scheduler.aclose()
