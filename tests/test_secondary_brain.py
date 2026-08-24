from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
import time
from dataclasses import replace
from types import MappingProxyType
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import friday.agent_runtime as agent_runtime_module
import friday.secondary_brain as secondary_brain_package
import friday.secondary_brain.client as secondary_client_module
import friday.secondary_brain.profiles as secondary_profiles
from friday.agent_runtime import AgentContext, AgentRuntime, _OwnedAttachment
from friday.agent_runtime.llm import LLMRouter
from friday.config import load_settings, validate_settings
from friday.secondary_brain import (
    EffectClass,
    ModelModality,
    ModelRequest,
    ModelWorkload,
    SecondaryEndpointConfig,
    SecondaryFailure,
    SecondaryResult,
    SecondaryState,
    build_secondary_brain,
)
from friday.secondary_brain.client import SecondaryEndpointClient
from friday.secondary_brain.profiles import SecondaryRuntimeProfile

_API_KEY = "a" * 64
_ENGINE_PROJECTION: dict[str, Any] = {
    "source_model_repository": "openai/gpt-oss-20b",
    "source_model_revision": "6cee5e81ee83917806bbde320786a8fb61efebee",
    "hardware_runtime_receipt_sha256": "0c1c9e6f54aa0004c3dfc89acd6904cfbb0f834d0988e971e34b9699b3d9031f",
    "source_model_manifest_sha256": "438df0a0b2f6b4164c2fd9d9ed309925abbc94ed8deb056b692d2ccad7887fd9",
    "runtime_image": "lmsysorg/sglang@sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405",
    "runtime_image_config_digest": "sha256:f7adc6c05df9ff711b82ad291cf1db6eaf30590c4d929833d632abfef3895efc",
    "runtime_image_oci_manifest_digest": "sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405",
    "runtime_source_revision": "29481685462732237d80d86076d6563e1f658102",
    "runtime_manifest_sha256": "e" * 64,
    "model_path": "/source/snapshot",
    "quantization": "mxfp4",
    "dtype": "bfloat16",
    "kv_cache_dtype": "bf16",
    "attention_backend": "triton",
    "moe_runner_backend": "flashinfer_mxfp4",
    "mxfp4_moe_precision": "default",
    "context_tokens": 4096,
    "max_total_tokens": 4096,
    "mem_fraction_static": "0.97",
    "max_running_requests": 1,
    "max_output_tokens": 512,
    "chunked_prefill_size": 1024,
    "cuda_graph_backend_decode": "disabled",
    "cuda_graph_backend_prefill": "disabled",
    "no_cpu_offload": True,
}
_ENGINE_BINDING_SHA256 = hashlib.sha256(
    (json.dumps(_ENGINE_PROJECTION, sort_keys=True, separators=(",", ":")) + "\n").encode()
).hexdigest()
_PROFILE_ID = f"gptoss20b-{_ENGINE_BINDING_SHA256}"
_ALIAS = f"friday-secondary-{_PROFILE_ID}"
_PROFILE_VALUE: dict[str, Any] = {
    **_ENGINE_PROJECTION,
    "schema": "friday.secondary-runtime-profile.v2",
    "status": "accepted",
    "profile_id": _PROFILE_ID,
    "engine_binding_sha256": _ENGINE_BINDING_SHA256,
    "endpoint_base_url": "http://127.0.0.1:30001/v1",
    "served_model_alias": _ALIAS,
    "gateway_ca_certificate_sha256": "",
    "allowed_modes": ["assist", "shadow"],
    "allowed_workloads": ["classify", "critique", "document_map"],
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


def _runtime_profile(**changes: Any) -> SecondaryRuntimeProfile:
    profile = SecondaryRuntimeProfile(
        profile_id=_PROFILE_ID,
        endpoint_base_url="http://127.0.0.1:30001/v1",
        served_model_alias=_ALIAS,
        manifest_sha256=_PROFILE_SHA256,
        engine_binding_sha256=_ENGINE_BINDING_SHA256,
        hardware_runtime_receipt_sha256=_ENGINE_PROJECTION["hardware_runtime_receipt_sha256"],
        gateway_ca_certificate_sha256="",
        max_context_tokens=4096,
        max_total_tokens=4096,
        max_concurrency=1,
        max_output_tokens=512,
        chunked_prefill_size=1024,
        mem_fraction_static="0.97",
        quantization="mxfp4",
        dtype="bfloat16",
        kv_cache_dtype="bf16",
        attention_backend="triton",
        moe_runner_backend="flashinfer_mxfp4",
        mxfp4_moe_precision="default",
        cuda_graph_backend_decode="disabled",
        cuda_graph_backend_prefill="disabled",
        no_cpu_offload=True,
        allowed_modes=frozenset({"shadow", "assist"}),
        allowed_workloads=frozenset({"classify", "critique", "document_map"}),
        model_repository="openai/gpt-oss-20b",
        model_revision="6cee5e81ee83917806bbde320786a8fb61efebee",
        source_model_manifest_sha256=_ENGINE_PROJECTION["source_model_manifest_sha256"],
        model_path="/source/snapshot",
        runtime_image=_ENGINE_PROJECTION["runtime_image"],
        runtime_image_config_digest=_ENGINE_PROJECTION["runtime_image_config_digest"],
        runtime_image_oci_manifest_digest=_ENGINE_PROJECTION["runtime_image_oci_manifest_digest"],
        runtime_source_revision=_ENGINE_PROJECTION["runtime_source_revision"],
        runtime_manifest_sha256="e" * 64,
    )
    return replace(profile, **changes)


@pytest.fixture(autouse=True)
def _accepted_test_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _runtime_profile()
    monkeypatch.setattr(
        secondary_profiles,
        "ACCEPTED_SECONDARY_RUNTIME_PROFILES",
        MappingProxyType({_PROFILE_ID: profile}),
    )


def _request(
    *,
    workload: ModelWorkload = ModelWorkload.CLASSIFY,
    messages: tuple[dict[str, Any], ...] = ({"role": "user", "content": "classify this"},),
    effect_class: EffectClass = EffectClass.NONE,
    modality: ModelModality = ModelModality.TEXT,
    structured: bool = False,
    private: bool = False,
) -> ModelRequest:
    return ModelRequest(
        workload=workload,
        messages=messages,
        max_output_tokens=64,
        absolute_deadline_monotonic=time.monotonic() + 5.0,
        effect_class=effect_class,
        modality=modality,
        require_structured_output=structured,
        contains_private_text=private,
    )


def _endpoint_config(*, cooldown_sec: float = 1.0) -> SecondaryEndpointConfig:
    return SecondaryEndpointConfig(
        base_url="http://127.0.0.1:30001/v1",
        served_model_alias=_ALIAS,
        api_key=_API_KEY,
        connect_timeout_sec=0.1,
        read_timeout_sec=0.5,
        call_budget_sec=1.0,
        admission_timeout_sec=0.01,
        cooldown_sec=cooldown_sec,
        max_context_tokens=4096,
        max_concurrency=1,
        max_output_tokens=512,
        profile_id=_PROFILE_ID,
        profile_manifest_sha256=_PROFILE_SHA256,
    )


def test_https_ca_bytes_are_code_pinned_before_client_construction(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pem = b"-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n"
    ca_file = tmp_path / "ca.crt"
    ca_file.write_bytes(pem)
    base = replace(
        _endpoint_config(),
        base_url="https://192.168.1.35:8443/v1",
        ca_file=str(ca_file),
        ca_sha256=hashlib.sha256(pem).hexdigest(),
    )
    assert base.is_complete is True
    assert replace(base, ca_sha256="0" * 64).is_complete is False
    (tmp_path / "wrong.crt").write_bytes(pem + b"unexpected")
    assert replace(base, ca_file=str(tmp_path / "wrong.crt")).is_complete is False

    loaded: dict[str, Any] = {}

    class ExactContext:
        verify_mode = ssl.CERT_REQUIRED
        check_hostname = True
        keylog_filename: str | None = None

        def load_verify_locations(self, *, cadata: str) -> None:
            loaded["cadata"] = cadata

    exact_context = ExactContext()
    monkeypatch.setattr(
        secondary_profiles,
        "ACCEPTED_SECONDARY_RUNTIME_PROFILES",
        MappingProxyType(
            {
                _PROFILE_ID: _runtime_profile(
                    endpoint_base_url=base.base_url,
                    gateway_ca_certificate_sha256=base.ca_sha256,
                )
            }
        ),
    )
    monkeypatch.setattr(secondary_client_module.ssl, "SSLContext", lambda _protocol: exact_context)
    monkeypatch.setattr(
        secondary_client_module.httpx,
        "AsyncClient",
        lambda **kwargs: loaded.update(kwargs) or object(),
    )
    SecondaryEndpointClient(base)
    assert loaded["verify"] is exact_context
    assert loaded["cadata"].encode("ascii") == pem
    assert exact_context.keylog_filename is None


def _response(
    *,
    model: str = _ALIAS,
    content: str = "accepted",
    message_extra: dict[str, Any] | None = None,
    finish_reason: str = "stop",
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    message.update(message_extra or {})
    return httpx.Response(
        200,
        headers=_PROFILE_HEADERS,
        json={
            "model": model,
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
    )


def _profile_response() -> httpx.Response:
    return httpx.Response(200, headers=_PROFILE_HEADERS, content=_PROFILE_BYTES)


def _models_response(*, alias: str = _ALIAS) -> httpx.Response:
    return httpx.Response(200, headers=_PROFILE_HEADERS, json={"data": [{"id": alias}]})


def _configured_settings(settings: Any, *, mode: str = "assist", private: bool = False) -> Any:
    return replace(
        settings,
        secondary_llm_enabled=True,
        secondary_llm_mode=mode,
        secondary_llm_base_url="http://127.0.0.1:30001/v1",
        secondary_llm_model=_ALIAS,
        secondary_llm_api_key=_API_KEY,
        secondary_llm_max_context_tokens=4096,
        secondary_llm_max_concurrency=1,
        secondary_llm_profile=_PROFILE_ID,
        secondary_llm_workloads=("classify", "critique"),
        secondary_llm_allow_private_text=private,
    )


def _after_admission(
    handler: Any,
) -> httpx.MockTransport:
    """Answer exact epoch probes, then delegate only the workload request."""

    async def admitted(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/friday-profile"):
            return _profile_response()
        if request.url.path.endswith("/models"):
            return _models_response()
        payload = __import__("json").loads(request.content)
        messages = payload.get("messages", [])
        if messages and messages[-1].get("content") == "Reply with exactly: ready":
            return _response(content="ready")
        return await handler(request)

    return httpx.MockTransport(admitted)


def test_transport_client_is_not_part_of_the_public_authority_surface() -> None:
    assert "SecondaryEndpointClient" not in secondary_brain_package.__all__
    assert not hasattr(secondary_brain_package, "SecondaryEndpointClient")


@pytest.mark.parametrize(
    "candidate",
    [
        replace(_endpoint_config(), base_url="https://public.example/v1", ca_file="/tmp/ca"),
        replace(_endpoint_config(), api_key="short-token"),
        replace(_endpoint_config(), max_concurrency=2),
        replace(_endpoint_config(), cooldown_sec=0.0),
        replace(_endpoint_config(), connect_timeout_sec=0.6, read_timeout_sec=0.5),
        replace(_endpoint_config(), admission_timeout_sec=0.5),
    ],
)
def test_endpoint_contract_fails_closed_outside_the_certified_boundary(
    candidate: SecondaryEndpointConfig,
) -> None:
    assert candidate.is_complete is False


def test_private_endpoint_requires_an_immutable_profile_binding(tmp_path: Any) -> None:
    ca_file = tmp_path / "ca.pem"
    ca_bytes = b"-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n"
    ca_file.write_bytes(ca_bytes)
    candidate = replace(
        _endpoint_config(),
        base_url="https://192.168.1.35:8443/v1",
        ca_file=str(ca_file),
        ca_sha256=hashlib.sha256(ca_bytes).hexdigest(),
        profile_id="",
        profile_manifest_sha256="",
    )
    assert candidate.is_complete is False
    assert (
        replace(
            candidate,
            profile_id="unknown-profile",
            profile_manifest_sha256="b" * 64,
        ).is_complete
        is True
    )


@pytest.mark.asyncio
async def test_profile_manifest_is_hashed_before_model_inventory() -> None:
    requested_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path.endswith("/friday-profile"):
            return _profile_response()
        return httpx.Response(
            200,
            headers=_PROFILE_HEADERS,
            json={"data": [{"id": _ALIAS}]},
        )

    client = SecondaryEndpointClient(_endpoint_config(), transport=httpx.MockTransport(handler))
    try:
        failure = await client.probe_models(absolute_deadline_monotonic=time.monotonic() + 2.0)
        assert failure is None
        assert requested_paths == ["/v1/friday-profile", "/v1/models"]
        assert client.status().profile_manifest_match is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_wrong_profile_manifest_fails_before_model_inventory() -> None:
    requested_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        return httpx.Response(200, content=b"wrong-profile")

    client = SecondaryEndpointClient(_endpoint_config(), transport=httpx.MockTransport(handler))
    try:
        failure = await client.probe_models(absolute_deadline_monotonic=time.monotonic() + 2.0)
        assert failure is SecondaryFailure.WRONG_PROFILE
        assert requested_paths == ["/v1/friday-profile"]
        assert client.status().profile_manifest_match is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_exact_hashed_candidate_profile_is_never_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = {**_PROFILE_VALUE, "status": "candidate"}
    for key in (
        "quality_evidence_sha256",
        "capacity_evidence_sha256",
        "soak_evidence_sha256",
        "failure_evidence_sha256",
    ):
        candidate[key] = "0" * 64
    candidate_bytes = (
        json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    requested_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        return httpx.Response(
            200,
            headers={
                "X-Friday-Secondary-Profile-Id": _PROFILE_ID,
                "X-Friday-Secondary-Profile-Sha256": hashlib.sha256(candidate_bytes).hexdigest(),
            },
            content=candidate_bytes,
        )

    config = replace(
        _endpoint_config(),
        profile_manifest_sha256=hashlib.sha256(candidate_bytes).hexdigest(),
    )
    accepted_profile = secondary_profiles.ACCEPTED_SECONDARY_RUNTIME_PROFILES[_PROFILE_ID]
    monkeypatch.setattr(
        secondary_profiles,
        "ACCEPTED_SECONDARY_RUNTIME_PROFILES",
        MappingProxyType(
            {
                _PROFILE_ID: replace(
                    accepted_profile,
                    manifest_sha256=config.profile_manifest_sha256,
                )
            }
        ),
    )
    client = SecondaryEndpointClient(config, transport=httpx.MockTransport(handler))
    try:
        failure = await client.probe_models(absolute_deadline_monotonic=time.monotonic() + 2.0)
        assert failure is SecondaryFailure.WRONG_PROFILE
        assert requested_paths == ["/v1/friday-profile"]
        assert client.status().profile_manifest_match is False
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "profile",
    [
        _runtime_profile(max_context_tokens=5000, max_total_tokens=5000),
        _runtime_profile(max_output_tokens=4096),
        _runtime_profile(max_context_tokens=True, max_total_tokens=True),
    ],
)
def test_product_profile_uses_the_deploy_capacity_bounds(profile: SecondaryRuntimeProfile) -> None:
    assert profile.is_well_formed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [],
        [
            ("X-Friday-Secondary-Profile-Id", _PROFILE_ID),
            ("X-Friday-Secondary-Profile-Sha256", "f" * 64),
        ],
        [
            ("X-Friday-Secondary-Profile-Id", _PROFILE_ID),
            ("X-Friday-Secondary-Profile-Id", _PROFILE_ID),
            ("X-Friday-Secondary-Profile-Sha256", _PROFILE_SHA256),
        ],
    ],
)
async def test_every_generation_requires_exact_single_profile_headers(
    headers: list[tuple[str, str]],
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        response = _response()
        return httpx.Response(200, headers=headers, content=response.content)

    client = SecondaryEndpointClient(_endpoint_config(), transport=httpx.MockTransport(handler))
    try:
        attempt = await client.call(_request())
        assert attempt.failure is SecondaryFailure.WRONG_PROFILE
        assert attempt.result is None
    finally:
        await client.aclose()


def test_secondary_settings_are_closed_and_inert_by_default(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRIDAY_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FRIDAY_ENV_FILE", str(tmp_path / "does-not-exist"))
    names = (
        "ENABLED",
        "MODE",
        "BASE_URL",
        "MODEL",
        "API_KEY",
        "CA_FILE",
        "CONNECT_TIMEOUT_SEC",
        "READ_TIMEOUT_SEC",
        "CALL_BUDGET_SEC",
        "ADMISSION_TIMEOUT_SEC",
        "HEALTH_INTERVAL_SEC",
        "COOLDOWN_SEC",
        "MAX_CONTEXT_TOKENS",
        "MAX_CONCURRENCY",
        "PROFILE",
        "WORKLOADS",
        "ALLOW_PRIVATE_TEXT",
    )
    for suffix in names:
        monkeypatch.delenv(f"FRIDAY_SECONDARY_LLM_{suffix}", raising=False)
        monkeypatch.delenv(f"JERICHO_SECONDARY_LLM_{suffix}", raising=False)

    loaded = load_settings()

    assert loaded.secondary_llm_enabled is False
    assert loaded.secondary_llm_mode == "disabled"
    assert loaded.secondary_llm_max_context_tokens == 0
    assert loaded.secondary_llm_profile == ""
    assert loaded.secondary_llm_allow_private_text is False

    monkeypatch.setenv("FRIDAY_SECONDARY_LLM_MODE", "typo-that-must-not-assist")
    assert load_settings().secondary_llm_mode == "disabled"


@pytest.mark.parametrize(
    "configured",
    [
        {"secondary_llm_profile": ""},
        {"secondary_llm_profile": "unknown-profile"},
        {"secondary_llm_max_context_tokens": 8192},
        {"secondary_llm_max_concurrency": 2},
        {"secondary_llm_workloads": ("extract",)},
    ],
)
def test_profile_mismatch_is_inert_before_transport(settings: Any, configured: dict[str, Any]) -> None:
    scheduler = build_secondary_brain(replace(_configured_settings(settings), **configured))
    try:
        assert scheduler.served_model_alias == ""
        assert scheduler.status().state is SecondaryState.MISCONFIGURED
    finally:
        asyncio.run(scheduler.aclose())


@pytest.mark.asyncio
async def test_disabled_builds_no_client_and_required_falls_back_exactly_once(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    constructed = 0

    class ForbiddenClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal constructed
            constructed += 1
            raise AssertionError("disabled secondary constructed a client")

    monkeypatch.setattr("friday.secondary_brain.scheduler.SecondaryEndpointClient", ForbiddenClient)
    scheduler = build_secondary_brain(settings)
    primary_calls = 0

    async def primary() -> str:
        nonlocal primary_calls
        primary_calls += 1
        return "primary"

    value = await scheduler.secondary_preferred_required_result(_request(), primary)
    optional = await scheduler.secondary_optional_advice(_request())
    shadow = await scheduler.run_shadow(_request, primary)

    assert value == "primary"
    assert optional is None
    assert shadow == "primary"
    assert primary_calls == 2
    assert constructed == 0
    assert scheduler.served_model_alias == ""
    assert scheduler.status().state is SecondaryState.DISABLED


@pytest.mark.parametrize(
    ("base_url", "ca_file"),
    [
        ("http://[/v1", ""),
        ("https://secondary.invalid/v1", "/definitely/missing/friday-secondary-ca.pem"),
    ],
)
def test_invalid_secondary_transport_configuration_is_fail_soft(
    settings: Any, base_url: str, ca_file: str
) -> None:
    configured = replace(
        _configured_settings(settings),
        secondary_llm_base_url=base_url,
        secondary_llm_ca_file=ca_file,
    )

    scheduler = build_secondary_brain(configured)

    assert scheduler.served_model_alias == ""
    assert scheduler.status().state is SecondaryState.MISCONFIGURED


@pytest.mark.parametrize(
    ("base_url", "ca_file"),
    [
        ("http://192.168.1.35:8443/v1", ""),
        ("https://192.168.1.35:8443/v1", ""),
    ],
)
def test_private_lan_requires_https_and_explicit_ca(settings: Any, base_url: str, ca_file: str) -> None:
    scheduler = build_secondary_brain(
        replace(
            _configured_settings(settings),
            secondary_llm_base_url=base_url,
            secondary_llm_ca_file=ca_file,
        )
    )
    assert scheduler.served_model_alias == ""
    assert scheduler.status().state is SecondaryState.MISCONFIGURED


@pytest.mark.asyncio
async def test_exact_alias_and_reasoning_are_sanitized(settings: Any) -> None:
    synthetic_reasoning = "internal-" + "scratchpad"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {_API_KEY}"
        payload = __import__("json").loads(request.content)
        assert payload["model"] == _ALIAS
        assert payload["reasoning_effort"] == "low"
        assert payload["temperature"] == 1.0
        assert payload["top_p"] == 1.0
        assert payload["seed"] == 0
        assert "tools" not in payload
        return _response(message_extra={"reasoning_content": synthetic_reasoning})

    scheduler = build_secondary_brain(
        _configured_settings(settings),
        transport=_after_admission(handler),
    )
    primary_calls = 0

    async def primary() -> str:
        nonlocal primary_calls
        primary_calls += 1
        return "primary"

    try:
        result = await scheduler.secondary_preferred_required_result(_request(), primary)
        assert isinstance(result, SecondaryResult)
        assert result.visible_content == "accepted"
        assert result.reasoning_was_separated is True
        assert synthetic_reasoning not in repr(result)
        assert synthetic_reasoning not in vars(result) if hasattr(result, "__dict__") else True
        assert primary_calls == 0
        assert scheduler.status().served_model_match is True
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_wrong_alias_uses_one_primary_fallback_without_retaining_raw_value(settings: Any) -> None:
    wrong_alias = "unexpected-" + "served-model"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return _response(model=wrong_alias)

    scheduler = build_secondary_brain(
        _configured_settings(settings),
        transport=_after_admission(handler),
    )
    primary_calls = 0

    async def primary() -> dict[str, str]:
        nonlocal primary_calls
        primary_calls += 1
        return {"source": "primary"}

    try:
        value = await scheduler.secondary_preferred_required_result(_request(), primary)
        assert value == {"source": "primary"}
        assert primary_calls == 1
        status = scheduler.status()
        assert status.last_failure is SecondaryFailure.WRONG_MODEL
        assert status.fallback_total == 1
        assert wrong_alias not in repr(status)
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_optional_failure_never_duplicates_primary_or_retains_exception(settings: Any) -> None:
    secret = "raw-transport-" + "exception"

    async def handler(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError(secret)

    scheduler = build_secondary_brain(
        _configured_settings(settings),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await scheduler.secondary_optional_advice(_request(workload=ModelWorkload.CRITIQUE))
        assert result is None
        assert scheduler.status().last_failure is SecondaryFailure.CONNECT_FAILED
        assert secret not in repr(scheduler.status())
        assert scheduler.status().fallback_total == 0
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_mid_submission_disconnect_falls_back_exactly_once(settings: Any) -> None:
    submitted = 0
    primary_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submitted
        submitted += 1
        raise httpx.ConnectError("synthetic disconnect", request=request)

    async def primary() -> str:
        nonlocal primary_calls
        primary_calls += 1
        return "primary"

    scheduler = build_secondary_brain(
        _configured_settings(settings),
        transport=_after_admission(handler),
    )
    try:
        assert await scheduler.secondary_preferred_required_result(_request(), primary) == "primary"
        assert submitted == 1
        assert primary_calls == 1
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_document_map_uses_accepted_4k_512_envelope_and_exact_primary_fallback(
    settings: Any,
    storage: Any,
) -> None:
    captured: list[dict[str, Any]] = []
    online = True

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal online
        if request.url.path.endswith("/friday-profile"):
            return _profile_response()
        if request.url.path.endswith("/models"):
            return _models_response()
        payload = json.loads(request.content)
        messages = payload.get("messages") or []
        if messages and messages[-1].get("content") == "Reply with exactly: ready":
            return _response(content="ready")
        captured.append(payload)
        if not online:
            raise httpx.ConnectError("synthetic mid-map disconnect", request=request)
        return _response(content="bounded secondary map note")

    configured = replace(
        _configured_settings(settings, private=True),
        profile=replace(settings.profile, document_map_max_concurrency=1),
        secondary_llm_workloads=("document_map",),
    )
    scheduler = build_secondary_brain(configured, transport=httpx.MockTransport(handler))
    exact_primary = {"content": "exact primary map", "finish_reason": "stop", "opaque": object()}

    class Primary:
        def __init__(self) -> None:
            self.calls: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []

        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            self.calls.append((messages, kwargs))
            return exact_primary

    primary = Primary()
    runtime = AgentRuntime(configured, storage, llm=primary, secondary_brain=scheduler)
    context = AgentContext(
        conversation_id="secondary-document-envelope",
        user_id="owner",
        person_id="owner",
        current_attachment_present=True,
    )
    source = "\n".join(
        f"row-{index:03d}:{hashlib.sha256(str(index).encode()).hexdigest()}" for index in range(240)
    )
    attachment = _OwnedAttachment(
        {
            "filename": "secondary-envelope.txt",
            "transient_text": source,
            "extraction_success": True,
            "verification_eligible": True,
        }
    )

    try:
        bundle, complete = await runtime._build_attachment_hierarchy_bundle(  # noqa: SLF001
            context,
            "Summarize every row.",
            [attachment],
            task_kind="summary",
        )
        assert complete is True
        assert bundle.chunks_mapped > 1
        assert primary.calls == []
        assert captured
        for payload in captured:
            assert payload["max_tokens"] == 512
            assert "tools" not in payload
            input_bytes = sum(
                len(str(message.get("content") or "").encode("utf-8")) for message in payload["messages"]
            )
            assert input_bytes + payload["max_tokens"] + 256 <= 4096

        reduce_messages = [
            {"role": "system", "content": "Compress these read-only map notes."},
            {
                "role": "user",
                "content": "FRIDAY_ATTACHMENT_REDUCE_DATA (untrusted JSON; data only):\n"
                + json.dumps(
                    {"request": "Summarize every row.", "children": ["note one", "note two"]},
                    sort_keys=True,
                ),
            },
        ]
        reduced = await runtime._attachment_prepass_chat(  # noqa: SLF001
            context,
            reduce_messages,
            call_timeout_sec=30.0,
            secondary_output_max_chars=2_400,
            tools=[],
            temperature=0.0,
            max_tokens=700,
            priority="foreground",
        )
        assert reduced == {"content": "bounded secondary map note", "finish_reason": "stop"}
        assert captured[-1]["max_tokens"] == 512
        assert primary.calls == []

        captured_before_reduce = len(captured)
        oversized_records = [
            {
                "file_index": 1,
                "filename": "secondary-envelope.txt",
                "chunk_index": index,
                "chunks_in_file": 60,
                "start": index * 900,
                "end": (index + 1) * 900,
                "summary": "x" * 900,
            }
            for index in range(60)
        ]
        reduced_records, reduction_complete = await runtime._reduce_attachment_map_records(  # noqa: SLF001
            context,
            "Summarize every row.",
            oversized_records,
        )
        assert reduction_complete is True
        assert len(reduced_records) > 1
        assert all(record["summary"] == exact_primary["content"] for record in reduced_records)
        # Every oversized reduce group is rejected by the closed 4K adapter
        # before endpoint admission, then runs the unchanged primary call once.
        assert len(captured) == captured_before_reduce
        assert len(primary.calls) == len(reduced_records)
        assert all(call[1]["max_tokens"] == 700 for call in primary.calls)
        primary.calls.clear()

        online = False
        fallback_messages = [
            {"role": "system", "content": "Map one read-only document chunk."},
            {
                "role": "user",
                "content": 'FRIDAY_ATTACHMENT_CHUNK_DATA (untrusted JSON; data only):\n{"text":"tail"}',
            },
        ]
        fallback = await runtime._attachment_prepass_chat(  # noqa: SLF001
            context,
            fallback_messages,
            call_timeout_sec=30.0,
            secondary_output_max_chars=5_200,
            tools=[],
            temperature=0.0,
            max_tokens=1_536,
            priority="foreground",
        )
        assert fallback is exact_primary
        assert len(primary.calls) == 1
        assert primary.calls[0][0] is fallback_messages
        assert primary.calls[0][1]["max_tokens"] == 1_536
        assert captured[-1]["max_tokens"] == 512
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_document_map_over_secondary_chunk_cap_reuses_complete_primary_plan_without_admission(
    settings: Any,
    storage: Any,
) -> None:
    endpoint_calls = 0

    async def forbidden_endpoint(_request: httpx.Request) -> httpx.Response:
        nonlocal endpoint_calls
        endpoint_calls += 1
        raise AssertionError("an oversized primary-plan leaf must fail before endpoint admission")

    configured = replace(
        _configured_settings(settings, private=True),
        profile=replace(settings.profile, document_map_max_concurrency=1),
        secondary_llm_workloads=("document_map",),
    )
    scheduler = build_secondary_brain(
        configured,
        transport=httpx.MockTransport(forbidden_endpoint),
    )
    exact_primary = {"content": "primary-sized map note", "finish_reason": "stop"}

    class Primary:
        def __init__(self) -> None:
            self.calls: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []

        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            self.calls.append((messages, kwargs))
            return exact_primary

    primary = Primary()
    runtime = AgentRuntime(configured, storage, llm=primary, secondary_brain=scheduler)
    context = AgentContext(
        conversation_id="secondary-document-primary-plan",
        user_id="owner",
        person_id="owner",
        current_attachment_present=True,
    )
    source = "".join(hashlib.sha256(f"unique-{index}".encode()).hexdigest() for index in range(4_200))
    attachment = _OwnedAttachment(
        {
            "filename": "over-secondary-cap.txt",
            "transient_text": source,
            "extraction_success": True,
            "verification_eligible": True,
        }
    )

    try:
        bundle, complete = await runtime._build_attachment_hierarchy_bundle(  # noqa: SLF001
            context,
            "Summarize the complete source.",
            [attachment],
            task_kind="summary",
        )
        assert complete is True
        assert bundle.source_chars_planned == bundle.source_chars_total == len(source)
        assert 1 < bundle.chunks_total < agent_runtime_module._ATTACHMENT_MAP_MAX_CHUNKS
        assert bundle.chunks_mapped == bundle.chunks_total
        assert len(primary.calls) == bundle.chunks_total
        assert all(call[1]["max_tokens"] == 1_536 for call in primary.calls)
        assert endpoint_calls == 0
        diagnostics = scheduler.diagnostics_status()
        assert diagnostics["selected_total"] == 0
        assert diagnostics["endpoint_request_total"] == 0
        assert diagnostics["primary_fallback_total"] == bundle.chunks_total
        assert diagnostics["fallback_reasons"] == {"context_exceeded": bundle.chunks_total}
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_deadline_hang_is_bounded_and_falls_back_exactly_once(settings: Any) -> None:
    primary_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def primary() -> str:
        nonlocal primary_calls
        primary_calls += 1
        return "primary"

    configured = replace(
        _configured_settings(settings),
        secondary_llm_connect_timeout_sec=0.05,
        secondary_llm_read_timeout_sec=0.05,
        secondary_llm_call_budget_sec=0.1,
    )
    scheduler = build_secondary_brain(configured, transport=_after_admission(handler))
    started = time.monotonic()
    try:
        assert await scheduler.secondary_preferred_required_result(_request(), primary) == "primary"
        assert time.monotonic() - started < 0.5
        assert primary_calls == 1
        assert scheduler.status().last_failure is SecondaryFailure.TIMEOUT
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_malformed_json_falls_back_exactly_once(settings: Any) -> None:
    primary_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=_PROFILE_HEADERS, content=b"{")

    async def primary() -> str:
        nonlocal primary_calls
        primary_calls += 1
        return "primary"

    scheduler = build_secondary_brain(
        _configured_settings(settings),
        transport=_after_admission(handler),
    )
    try:
        assert await scheduler.secondary_preferred_required_result(_request(), primary) == "primary"
        assert primary_calls == 1
        assert scheduler.status().last_failure is SecondaryFailure.MALFORMED_RESPONSE
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_mutating_request_runs_primary_effect_once_without_secondary_replay(settings: Any) -> None:
    network_calls = 0
    effect_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return _response()

    async def primary_effect() -> str:
        nonlocal effect_calls
        effect_calls += 1
        return "effect-result"

    scheduler = build_secondary_brain(
        _configured_settings(settings),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await scheduler.secondary_preferred_required_result(
            _request(effect_class=EffectClass.MUTATING),
            primary_effect,
        )
        assert result == "effect-result"
        assert network_calls == 0
        assert effect_calls == 1
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate", "failure"),
    [
        (
            _request(
                messages=(
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": "data:image/x"}}],
                    },
                )
            ),
            SecondaryFailure.UNSUPPORTED_MODALITY,
        ),
        (
            _request(effect_class=EffectClass.MUTATING),
            SecondaryFailure.EFFECT_DENIED,
        ),
        (
            _request(private=True),
            SecondaryFailure.PRIVATE_TEXT_DISALLOWED,
        ),
        (
            _request(
                messages=(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"id": "must-never-reach-secondary"}],
                    },
                )
            ),
            SecondaryFailure.TOOL_CALL_REJECTED,
        ),
        (
            _request(messages=({"role": "user", "content": "🙂" * 1_000},)),
            SecondaryFailure.CONTEXT_EXCEEDED,
        ),
    ],
)
async def test_policy_rejections_make_no_network_request(
    settings: Any, candidate: ModelRequest, failure: SecondaryFailure
) -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _response()

    scheduler = build_secondary_brain(
        _configured_settings(settings),
        transport=httpx.MockTransport(handler),
    )
    try:
        attempt = await scheduler.attempt(candidate)
        assert attempt.failure is failure
        assert requests == 0
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        f"copied endpoint credential: {_API_KEY}",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
        "OLD_API_KEY=abcdefghijklmnopqrstuvwxyz0123456789",
        "PATH=/tmp\nUSER=tester\nLANG=ru_RU.UTF-8",
        "-----BEGIN PRIVATE KEY-----\nnot-for-a-model",
    ],
)
async def test_credential_and_environment_shapes_are_rejected_before_probe(
    settings: Any,
    content: str,
) -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _response()

    scheduler = build_secondary_brain(
        _configured_settings(settings, private=True),
        transport=httpx.MockTransport(handler),
    )
    try:
        attempt = await scheduler.attempt(
            _request(messages=({"role": "user", "content": content},), private=True)
        )
        assert attempt.failure is SecondaryFailure.SECRET_MATERIAL_DENIED
        assert requests == 0
        assert content not in repr(scheduler.diagnostics_status())
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_secondary_admission_is_immediate_and_does_not_queue() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return _response()

    client = SecondaryEndpointClient(
        _endpoint_config(),
        transport=httpx.MockTransport(handler),
    )
    first = asyncio.create_task(client.call(_request()))
    try:
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        started = time.monotonic()
        second = await client.call(_request())
        elapsed = time.monotonic() - started
        assert second.failure is SecondaryFailure.ADMISSION_BUSY
        assert elapsed < 0.05
        release.set()
        assert (await first).succeeded
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_clients_have_independent_circuits() -> None:
    async def broken(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async def healthy(_request: httpx.Request) -> httpx.Response:
        return _response()

    broken_client = SecondaryEndpointClient(_endpoint_config(), transport=httpx.MockTransport(broken))
    healthy_client = SecondaryEndpointClient(_endpoint_config(), transport=httpx.MockTransport(healthy))
    try:
        assert (await broken_client.call(_request())).failure is SecondaryFailure.HTTP_TRANSIENT
        assert broken_client.status().state is SecondaryState.COOLDOWN
        assert (await healthy_client.call(_request())).succeeded
        assert healthy_client.status().state is SecondaryState.HEALTHY
    finally:
        await broken_client.aclose()
        await healthy_client.aclose()


@pytest.mark.asyncio
async def test_cooldown_admits_only_one_half_open_probe() -> None:
    now = [0.0]
    calls = 0
    probe_entered = asyncio.Event()
    probe_release = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        probe_entered.set()
        await probe_release.wait()
        return _response()

    client = SecondaryEndpointClient(
        _endpoint_config(cooldown_sec=1.0),
        transport=httpx.MockTransport(handler),
        clock=lambda: now[0],
    )
    try:
        assert (await client.call(_request())).failure is SecondaryFailure.HTTP_TRANSIENT
        now[0] = 2.0
        probe = asyncio.create_task(client.call(_request()))
        await asyncio.wait_for(probe_entered.wait(), timeout=0.5)
        rejected = await client.call(_request())
        assert rejected.failure is SecondaryFailure.COOLDOWN
        assert calls == 2
        probe_release.set()
        assert (await probe).succeeded
    finally:
        probe_release.set()
        await client.aclose()


@pytest.mark.asyncio
async def test_cancellation_drains_local_http_task() -> None:
    entered = asyncio.Event()
    drained = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            drained.set()
            raise
        raise AssertionError("unreachable")

    client = SecondaryEndpointClient(
        _endpoint_config(),
        transport=httpx.MockTransport(handler),
    )
    task = asyncio.create_task(client.call(_request()))
    try:
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert drained.is_set()
        assert client.status().active_requests == 0
        assert client.status().last_failure is SecondaryFailure.CANCELLED
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_shadow_guarantees_primary_then_discards_secondary(settings: Any) -> None:
    order: list[str] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        order.append("secondary")
        return _response(content="must be discarded")

    scheduler = build_secondary_brain(
        _configured_settings(settings, mode="shadow"),
        transport=_after_admission(handler),
    )

    async def primary() -> object:
        order.append("primary")
        return {"identity": object()}

    try:
        primary_value = await primary()

        async def same_primary() -> object:
            order.clear()
            order.append("primary")
            return primary_value

        result = await scheduler.run_shadow(_request, same_primary)
        assert result is primary_value
        await scheduler.drain_shadow()
        assert order == ["primary", "secondary"]
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_structured_result_is_typed_and_tool_output_is_rejected(settings: Any) -> None:
    responses = [
        _response(content='{"label":"ok","score":1}'),
        _response(message_extra={"tool_calls": [{"id": "never-execute"}]}),
    ]

    async def handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    scheduler = build_secondary_brain(
        _configured_settings(settings),
        transport=_after_admission(handler),
    )
    try:
        structured = await scheduler.attempt(_request(structured=True))
        assert structured.result is not None
        assert structured.result.structured_output == {"label": "ok", "score": 1}
        tool_attempt = await scheduler.attempt(_request())
        assert tool_attempt.result is None
        assert tool_attempt.failure is SecondaryFailure.TOOL_CALL_REJECTED
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_response",
    [
        _response(content="<|analysis|>hidden<|final|>visible"),
        _response(content="<|constrain|>json"),
        _response(content="<think>hidden</think>visible"),
        _response(content='{"label":"partial"}', finish_reason="length"),
    ],
)
async def test_harmony_markers_and_incomplete_finishes_are_rejected(
    settings: Any, unsafe_response: httpx.Response
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return unsafe_response

    scheduler = build_secondary_brain(
        _configured_settings(settings),
        transport=_after_admission(handler),
    )
    try:
        attempt = await scheduler.attempt(_request())
        assert attempt.result is None
        assert attempt.failure in {
            SecondaryFailure.REASONING_LEAK,
            SecondaryFailure.MALFORMED_RESPONSE,
        }
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_bad_process_epoch_canary_prevents_workload_admission(settings: Any) -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/friday-profile"):
            return _profile_response()
        if request.url.path.endswith("/models"):
            return _models_response()
        return _response(content="almost ready")

    scheduler = build_secondary_brain(
        _configured_settings(settings),
        transport=httpx.MockTransport(handler),
    )
    try:
        attempt = await scheduler.attempt(_request())
        assert attempt.result is None
        assert attempt.failure is SecondaryFailure.MALFORMED_RESPONSE
        assert paths == ["/v1/friday-profile", "/v1/models", "/v1/chat/completions"]
        assert scheduler.status().state is SecondaryState.COOLDOWN
        diagnostics: Any = scheduler.diagnostics_status()
        assert diagnostics["workloads"]["classify"]["selected_total"] == 0
        assert diagnostics["probe_failure_total"] == 1
        assert diagnostics["probe_failure_reasons"] == {"malformed_response": 1}
        assert diagnostics["protocol_rejection_total"] == 1
        assert diagnostics["protocol_rejection_reasons"] == {"malformed_response": 1}
        assert diagnostics["queue_wait"]["count"] == 2
        assert diagnostics["queue_wait"]["sum_sec"] >= 0.0
        assert diagnostics["queue_wait"]["max_sec"] >= 0.0
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_workload_never_queues_behind_process_epoch_probe(settings: Any) -> None:
    probe_entered = asyncio.Event()
    release_probe = asyncio.Event()
    workload_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal workload_calls
        if request.url.path.endswith("/friday-profile"):
            return _profile_response()
        if request.url.path.endswith("/models"):
            probe_entered.set()
            await release_probe.wait()
            return _models_response()
        payload = __import__("json").loads(request.content)
        if payload["messages"][-1]["content"] == "Reply with exactly: ready":
            return _response(content="ready")
        workload_calls += 1
        return _response()

    scheduler = build_secondary_brain(
        replace(_configured_settings(settings), secondary_llm_admission_timeout_sec=0.01),
        transport=httpx.MockTransport(handler),
    )
    try:
        scheduler.start()
        await asyncio.wait_for(probe_entered.wait(), timeout=0.5)
        started = time.monotonic()
        attempt = await scheduler.attempt(_request())
        assert attempt.failure is SecondaryFailure.ADMISSION_BUSY
        assert time.monotonic() - started < 0.1
        assert workload_calls == 0
        workload = scheduler.diagnostics_status()["workloads"]["classify"]
        assert workload["queue_wait_count"] == 1
        assert workload["queue_wait_sum_sec"] > 0.0
        assert workload["queue_wait_max_sec"] > 0.0
    finally:
        release_probe.set()
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_process_epoch_probe_checks_models_then_generation(settings: Any) -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/friday-profile"):
            return _profile_response()
        if request.url.path.endswith("/models"):
            return _models_response()
        return _response(content="ready")

    scheduler = build_secondary_brain(
        _configured_settings(settings),
        transport=httpx.MockTransport(handler),
    )
    try:
        scheduler.start()
        for _ in range(100):
            if scheduler.status().state is SecondaryState.HEALTHY and len(paths) == 3:
                break
            await asyncio.sleep(0)
        assert paths == ["/v1/friday-profile", "/v1/models", "/v1/chat/completions"]
        assert scheduler.status().state is SecondaryState.HEALTHY
        assert scheduler.status().served_model_match is True
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_stale_health_refresh_does_not_repeat_process_epoch_generation_canary(
    settings: Any,
) -> None:
    paths: list[str] = []
    canaries = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal canaries
        paths.append(request.url.path)
        if request.url.path.endswith("/friday-profile"):
            return _profile_response()
        if request.url.path.endswith("/models"):
            return _models_response()
        payload = __import__("json").loads(request.content)
        if payload["messages"][-1]["content"] == "Reply with exactly: ready":
            canaries += 1
            return _response(content="ready")
        return _response(content="accepted")

    scheduler = build_secondary_brain(
        replace(_configured_settings(settings), secondary_llm_health_interval_sec=0.001),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert (await scheduler.attempt(_request())).result is not None
        await asyncio.sleep(0.003)
        assert (await scheduler.attempt(_request())).result is not None
        assert canaries == 1
        workload_metrics = scheduler.diagnostics_status()["workloads"]["classify"]
        assert workload_metrics["queue_wait_count"] == 2
        assert workload_metrics["queue_wait_sum_sec"] >= 0.0
        assert workload_metrics["queue_wait_max_sec"] >= 0.0
        assert paths == [
            "/v1/friday-profile",
            "/v1/models",
            "/v1/chat/completions",
            "/v1/chat/completions",
            "/v1/friday-profile",
            "/v1/models",
            "/v1/chat/completions",
        ]
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_wrong_models_inventory_opens_cooldown_without_generation(settings: Any) -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/friday-profile"):
            return _profile_response()
        return _models_response(alias="wrong-alias")

    scheduler = build_secondary_brain(
        _configured_settings(settings),
        transport=httpx.MockTransport(handler),
    )
    try:
        scheduler.start()
        for _ in range(100):
            if scheduler.status().state is SecondaryState.COOLDOWN:
                break
            await asyncio.sleep(0)
        assert paths == ["/v1/friday-profile", "/v1/models"]
        assert scheduler.status().state is SecondaryState.COOLDOWN
        assert scheduler.status().last_failure is SecondaryFailure.WRONG_MODEL
        diagnostics = scheduler.diagnostics_status()
        assert diagnostics["protocol_rejection_total"] == 1
        assert diagnostics["protocol_rejection_reasons"] == {"wrong_model": 1}
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_laptop_is_readmitted_on_demand_without_primary_restart(settings: Any) -> None:
    online = False
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if not online:
            raise httpx.ConnectError("synthetic laptop offline", request=request)
        if request.url.path.endswith("/friday-profile"):
            return _profile_response()
        if request.url.path.endswith("/models"):
            return _models_response()
        payload = __import__("json").loads(request.content)
        if payload["messages"][-1]["content"] == "Reply with exactly: ready":
            return _response(content="ready")
        return _response(content="recovered")

    scheduler = build_secondary_brain(
        replace(_configured_settings(settings), secondary_llm_cooldown_sec=0.001),
        transport=httpx.MockTransport(handler),
    )
    try:
        first = await scheduler.attempt(_request())
        assert first.failure is SecondaryFailure.CONNECT_FAILED
        online = True
        await asyncio.sleep(0.002)
        second = await scheduler.attempt(_request())
        assert second.result is not None
        assert second.result.visible_content == "recovered"
        assert paths == [
            "/v1/friday-profile",
            "/v1/friday-profile",
            "/v1/models",
            "/v1/chat/completions",
            "/v1/chat/completions",
        ]
        assert scheduler.public_status()["available"] is True
    finally:
        await scheduler.aclose()


def test_secondary_public_projection_never_contains_endpoint_or_credentials(settings: Any) -> None:
    api_key = "b" * 64
    endpoint = "http://127.0.0.1:30001/v1"
    configured = replace(
        _configured_settings(settings),
        secondary_llm_base_url=endpoint,
        secondary_llm_api_key=api_key,
    )
    scheduler = build_secondary_brain(configured)
    try:
        projection = scheduler.public_status()
        public_settings = configured.public_dict()["secondary_llm"]
        assert projection["role"] == "optional_advisory"
        assert projection["state"] == "probing"
        assert projection["available"] is False
        diagnostics = scheduler.diagnostics_status()
        assert diagnostics["selected_total"] == 0
        serialized = repr((projection, diagnostics, public_settings))
        assert api_key not in serialized
        assert endpoint not in serialized
    finally:
        asyncio.run(scheduler.aclose())


def test_incomplete_optional_configuration_is_only_a_warning(settings: Any) -> None:
    configured = replace(
        settings,
        secondary_llm_enabled=True,
        secondary_llm_mode="assist",
        secondary_llm_base_url="",
        secondary_llm_model="",
        secondary_llm_api_key="",
        secondary_llm_max_context_tokens=0,
    )

    issues = validate_settings(configured)

    assert any(issue.startswith("warning:") and "optional secondary" in issue for issue in issues)
    assert not any(not issue.startswith("warning:") and "SECONDARY" in issue for issue in issues)


def test_same_primary_identity_or_unsafe_secondary_timing_is_fail_soft(settings: Any) -> None:
    same_endpoint = build_secondary_brain(
        replace(_configured_settings(settings), secondary_llm_base_url=settings.llm_base_url)
    )
    unsafe_timing = build_secondary_brain(
        replace(
            _configured_settings(settings),
            llm_timeout_sec=10.0,
            secondary_llm_read_timeout_sec=10.0,
            secondary_llm_call_budget_sec=10.0,
        )
    )
    try:
        assert same_endpoint.served_model_alias == ""
        assert unsafe_timing.served_model_alias == ""
        assert same_endpoint.status().state is SecondaryState.MISCONFIGURED
        assert unsafe_timing.status().state is SecondaryState.MISCONFIGURED
    finally:
        asyncio.run(same_endpoint.aclose())
        asyncio.run(unsafe_timing.aclose())


@pytest.mark.asyncio
async def test_optional_transport_close_is_idempotent_and_fail_soft(settings: Any) -> None:
    class ExplodingCloseTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, _request: httpx.Request) -> httpx.Response:
            return _response()

        async def aclose(self) -> None:
            raise RuntimeError("synthetic close failure")

    scheduler = build_secondary_brain(
        _configured_settings(settings),
        transport=ExplodingCloseTransport(),
    )
    await scheduler.aclose()
    await scheduler.aclose()


def test_malformed_optional_url_never_breaks_primary_validation(settings: Any) -> None:
    configured = replace(
        _configured_settings(settings),
        secondary_llm_base_url="http://[/v1",
    )

    issues = validate_settings(configured)

    assert any(issue.startswith("warning:") and "secondary" in issue for issue in issues)
    assert not any(not issue.startswith("warning:") and "secondary" in issue for issue in issues)


def test_server_stays_healthy_with_secondary_disabled_or_laptop_off(settings: Any) -> None:
    from friday.server import create_app

    for configured, expected_states in (
        (settings, {"disabled"}),
        (
            _configured_settings(settings),
            {"probing", "cooldown"},
        ),
    ):
        app = create_app(configured)
        with TestClient(app) as client:
            payload = client.get("/api/health").json()
            assert payload["status"] == "ok"
            assert payload["secondary"]["role"] == "optional_advisory"
            assert payload["secondary"]["state"] in expected_states
            assert payload["secondary"]["available"] is False
            assert type(app.state.llm) is LLMRouter
            owner = {"Authorization": f"Bearer {configured.api_token}"}
            diagnostics = client.get("/api/admin/diagnostics", headers=owner).json()
            assert diagnostics["ok"] is True
            assert diagnostics["secondary"]["state"] in expected_states


def test_enabled_secondary_cannot_change_primary_v12_identity(settings: Any) -> None:
    from friday.v12_model_transport import create_attested_v12_model_runtime

    primary_settings = replace(settings, llm_enabled=True)
    secondary_settings = replace(_configured_settings(settings), llm_enabled=True)
    primary_router = LLMRouter(primary_settings)
    secondary_router = LLMRouter(secondary_settings)
    primary_runtime = create_attested_v12_model_runtime(primary_router)
    secondary_runtime = create_attested_v12_model_runtime(secondary_router)

    assert secondary_router.model == primary_router.model
    assert secondary_router.model != secondary_settings.secondary_llm_model
    assert secondary_runtime.profile is primary_runtime.profile
    assert secondary_runtime.profile.served_model_alias == primary_router.model
    assert secondary_runtime.public_status() == primary_runtime.public_status()
