from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import replace
from typing import Any

import httpx
import pytest

from friday.agent_runtime.llm import LLMRouter
from friday.config import PROFILES
from friday.model_probe import ModelProbeError, ModelProbeFailure
from friday.model_profiles import (
    QWEN36_27B_V12_PROFILE,
    QWEN38_27B_SGLANG_V12_PROFILE,
)
from friday.v12_model_runtime import (
    MAX_DEPLOYMENT_WITNESS_BYTES,
    MAX_SGLANG_METRICS_BYTES,
    MAX_VLLM_METRICS_BYTES,
    V12ModelRuntimeError,
    V12ModelRuntimeFailure,
    _parse_metrics,
    _parse_sglang_deployment_witness,
    _parse_sglang_metrics,
    _parse_sglang_server_info,
)
from friday.v12_model_transport import (
    V12ModelTransportError,
    V12ModelTransportFailure,
    create_attested_v12_model_runtime,
)

_LABELS = 'engine_type="unified",model_name="dispatcher",moe_ep_rank="0",pp_rank="0",tp_rank="0"'


def _bounded_value_contains_marker(value: object, marker: str, seen: set[int]) -> bool:
    identity = id(value)
    if identity in seen:
        return False
    seen.add(identity)
    if isinstance(value, str):
        return marker in value
    if isinstance(value, bytes):
        return marker.encode() in value
    if isinstance(value, dict):
        return any(
            _bounded_value_contains_marker(item, marker, seen) for pair in value.items() for item in pair
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_bounded_value_contains_marker(item, marker, seen) for item in value)
    return False


def _exception_traceback_contains_marker(error: BaseException, marker: str) -> bool:
    pending: list[BaseException] = [error]
    seen_errors: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen_errors:
            continue
        seen_errors.add(id(current))
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
        traceback = current.__traceback__
        while traceback is not None:
            module_name = str(traceback.tb_frame.f_globals.get("__name__", ""))
            if module_name == "friday" or module_name.startswith("friday."):
                for value in traceback.tb_frame.f_locals.values():
                    if _bounded_value_contains_marker(value, marker, set()):
                        return True
            traceback = traceback.tb_next
    return False


def _sglang_metrics(*, running: str = "0.0", waiting: str = "0.0") -> bytes:
    return (
        f"sglang:num_running_reqs{{{_LABELS}}} {running}\nsglang:num_queue_reqs{{{_LABELS}}} {waiting}\n"
    ).encode()


def _metrics_body_68k() -> bytes:
    target = 68_064
    required = _sglang_metrics(running="2", waiting="3")
    padding_size = target - len(required)
    full_lines, remainder = divmod(padding_size, len(b"# pad\n"))
    padding = b"# pad\n" * full_lines
    if remainder:
        padding += b"#" + (b"x" * (remainder - 2)) + b"\n"
    body = required + padding
    assert len(body) == target
    assert MAX_VLLM_METRICS_BYTES < len(body) < MAX_SGLANG_METRICS_BYTES
    return body


def _server_info(*, random_seed: int = 786_846_033, **changes: Any) -> bytes:
    runtime_profile = PROFILES["qwen38-27b-nvfp4-sglang"]
    launch = runtime_profile.sglang_extra_args
    assert launch is not None
    value: dict[str, Any] = {
        "status": "ready",
        "version": runtime_profile.runtime_reported_version,
        "model_path": f"/models/{runtime_profile.model_dir_name}",
        "served_model_name": "dispatcher",
        "random_seed": random_seed,
        "context_length": runtime_profile.max_model_len,
        "max_running_requests": runtime_profile.max_num_seqs,
        "max_total_tokens": launch.max_total_tokens,
        "max_total_num_tokens": launch.max_total_tokens,
        "mem_fraction_static": launch.mem_fraction_static,
        "kv_cache_dtype": runtime_profile.kv_cache_dtype,
        "chunked_prefill_size": launch.chunked_prefill_size,
        "mamba_ssm_dtype": launch.mamba_ssm_dtype,
        "max_mamba_cache_size": launch.max_mamba_cache_size,
        "disable_radix_cache": not launch.radix_cache_enabled,
        "disable_cuda_graph": runtime_profile.eager_mode,
        "cuda_graph_backend_decode": launch.cuda_graph_backend_decode,
        "cuda_graph_max_bs_decode": launch.cuda_graph_max_bs_decode,
        "cuda_graph_bs_decode": list(launch.cuda_graph_bs_decode),
        "cuda_graph_backend_prefill": launch.cuda_graph_backend_prefill,
        "attention_backend": launch.attention_backend,
        "reasoning_parser": launch.reasoning_parser,
        "tool_call_parser": launch.tool_call_parser,
        "mm_feature_transport": launch.mm_feature_transport,
        "limit_mm_data_per_request": json.loads(launch.limit_mm_data_per_request),
        "enable_metrics": launch.metrics_enabled,
        "weight_version": launch.weight_version,
        "speculative_algorithm": launch.speculative_algorithm,
        "speculative_draft_model_path": None,
        "speculative_num_steps": None,
        # SGLang exposes these fields, but they are deliberately outside the
        # retained epoch projection.
        "api_key": "secret-one",
        "admin_api_key": "secret-two",
        "ssl_keyfile_password": "secret-three",
    }
    value.update(changes)
    return json.dumps(value, separators=(",", ":")).encode()


def _deployment_witness(
    *,
    random_seed: int = 786_846_033,
    nonce: str = "a" * 64,
    reverse_keys: bool = False,
    **changes: Any,
) -> bytes:
    runtime_profile = PROFILES["qwen38-27b-nvfp4-sglang"]
    value: dict[str, Any] = {
        "schema": "friday.sglang-deployment-witness.v1",
        "profile_id": QWEN38_27B_SGLANG_V12_PROFILE.profile_id,
        "engine_start_nonce": nonce,
        "engine_random_seed": random_seed,
        "engine_image_id": runtime_profile.engine_image_id,
        "engine_base_image_digest": runtime_profile.engine_base_image_digest,
        "engine_base_image_id": runtime_profile.engine_base_image_id,
        "runtime_source_revision": runtime_profile.runtime_source_revision,
        "runtime_reported_version": runtime_profile.runtime_reported_version,
        "model_repository": runtime_profile.model_repository,
        "model_revision": runtime_profile.model_revision,
        "model_snapshot_manifest_sha256": runtime_profile.model_snapshot_manifest_sha256,
        "model_quantization": runtime_profile.model_quantization,
        "served_model_alias": QWEN38_27B_SGLANG_V12_PROFILE.served_model_alias,
        "launch_manifest_sha256": runtime_profile.launch_manifest_sha256,
        "proxy_image_id": runtime_profile.proxy_image_id,
        "proxy_policy_sha256": runtime_profile.proxy_policy_sha256,
    }
    value.update(changes)
    if reverse_keys:
        value = dict(reversed(tuple(value.items())))
    return json.dumps(value, separators=(",", ":")).encode()


def _router(settings, *, q38: bool = True, **changes: Any) -> LLMRouter:
    values = {
        "profile": PROFILES["qwen38-27b-nvfp4-sglang" if q38 else "qwen36-27b-nvfp4-nvidia"],
        "llm_enabled": True,
        "llm_model": "dispatcher",
        "llm_api_key": "private-v12-key",
        "llm_base_url": "http://127.0.0.1:8001/v1",
        **changes,
    }
    configured = replace(settings, **values)
    return LLMRouter(configured)


@pytest.mark.parametrize(
    "body",
    [
        _sglang_metrics().splitlines(keepends=True)[0],
        _sglang_metrics() + _sglang_metrics().splitlines(keepends=True)[0],
        _sglang_metrics().replace(b'model_name="dispatcher"', b'model_name="wrong"'),
        _sglang_metrics().replace(b'moe_ep_rank="0",', b""),
        _sglang_metrics().replace(b'tp_rank="0"', b'tp_rank="1"'),
        _sglang_metrics().replace(b'engine_type="unified"', b'engine_type="decode"'),
        _sglang_metrics(running="0.5"),
        _sglang_metrics(waiting="NaN"),
    ],
    ids=(
        "missing",
        "duplicate",
        "wrong-model",
        "missing-label",
        "wrong-rank",
        "wrong-engine",
        "fractional",
        "nonfinite",
    ),
)
def test_sglang_metrics_require_exact_unique_samples_and_labels(body: bytes) -> None:
    with pytest.raises(V12ModelRuntimeError) as caught:
        _parse_sglang_metrics(body, served_model_alias="dispatcher")

    assert caught.value.code is V12ModelRuntimeFailure.METRICS_INVALID


def test_sglang_metrics_reject_body_over_128k_bound() -> None:
    body = _sglang_metrics() + b"# pad\n" * 22_000
    assert len(body) > MAX_SGLANG_METRICS_BYTES

    with pytest.raises(V12ModelRuntimeError) as caught:
        _parse_sglang_metrics(body, served_model_alias="dispatcher")

    assert caught.value.code is V12ModelRuntimeFailure.METRICS_INVALID


def test_sglang_server_info_projection_is_secret_free_and_seed_bound() -> None:
    secret = "do-not-retain-this-server-secret"
    first = _parse_sglang_server_info(
        _server_info(api_key=secret),
        profile=QWEN38_27B_SGLANG_V12_PROFILE,
    )
    same = _parse_sglang_server_info(
        _server_info(api_key="different-secret", unknown_private_field=secret),
        profile=QWEN38_27B_SGLANG_V12_PROFILE,
    )
    drifted = _parse_sglang_server_info(
        _server_info(random_seed=786_846_034, api_key=secret),
        profile=QWEN38_27B_SGLANG_V12_PROFILE,
    )

    assert first.random_seed == same.random_seed == 786_846_033
    assert drifted.random_seed == 786_846_034
    assert secret not in repr(first)


@pytest.mark.parametrize(
    "change",
    [
        {"version": "wrong-runtime"},
        {"model_path": "/models/wrong"},
        {"context_length": 32_768},
        {"max_running_requests": 7},
        {"disable_cuda_graph": True},
        {"speculative_algorithm": "mtp"},
        {"weight_version": "changed"},
    ],
    ids=(
        "runtime",
        "model",
        "context",
        "capacity",
        "graph",
        "speculation",
        "weight-version",
    ),
)
def test_sglang_server_info_rejects_wrong_runtime_without_secret_leak(
    change: dict[str, object],
) -> None:
    secret = "private-server-info-sentinel"
    with pytest.raises(V12ModelRuntimeError) as caught:
        _parse_sglang_server_info(
            _server_info(api_key=secret, **change),
            profile=QWEN38_27B_SGLANG_V12_PROFILE,
        )

    assert caught.value.code is V12ModelRuntimeFailure.METRICS_INVALID
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


def test_sglang_server_info_rejects_duplicate_keys_and_wrong_profile() -> None:
    body = _server_info()
    duplicate = body[:-1] + b',"random_seed":1}'
    for value, profile in (
        (duplicate, QWEN38_27B_SGLANG_V12_PROFILE),
        (body, QWEN36_27B_V12_PROFILE),
    ):
        with pytest.raises(V12ModelRuntimeError):
            _parse_sglang_server_info(value, profile=profile)


def test_deployment_witness_epoch_is_canonical_and_nonce_bound() -> None:
    first = _parse_sglang_deployment_witness(
        _deployment_witness(),
        profile=QWEN38_27B_SGLANG_V12_PROFILE,
    )
    reordered = _parse_sglang_deployment_witness(
        _deployment_witness(reverse_keys=True),
        profile=QWEN38_27B_SGLANG_V12_PROFILE,
    )
    restarted = _parse_sglang_deployment_witness(
        _deployment_witness(nonce="b" * 64),
        profile=QWEN38_27B_SGLANG_V12_PROFILE,
    )

    assert first == reordered
    assert first != restarted
    assert first.process_epoch_sha256 == hashlib.sha256(first.canonical_json).hexdigest()
    assert first.engine_random_seed == 786_846_033


@pytest.mark.parametrize(
    "body",
    [
        _deployment_witness().replace(
            b'"schema":"friday.sglang-deployment-witness.v1",',
            b"",
        ),
        _deployment_witness()[:-1] + b',"extra":"private-secret"}',
        _deployment_witness()[:-1] + b',"engine_random_seed":1}',
        _deployment_witness(engine_image_id="wrong"),
        _deployment_witness(nonce="A" * 64),
        _deployment_witness(random_seed=True),
        b"x" * (MAX_DEPLOYMENT_WITNESS_BYTES + 1),
    ],
    ids=(
        "missing",
        "extra",
        "duplicate",
        "wrong-identity",
        "wrong-nonce",
        "boolean-seed",
        "oversized",
    ),
)
def test_deployment_witness_rejects_non_exact_or_ambiguous_identity(body: bytes) -> None:
    secret = "private-secret"
    with pytest.raises(V12ModelRuntimeError) as caught:
        _parse_sglang_deployment_witness(
            body,
            profile=QWEN38_27B_SGLANG_V12_PROFILE,
        )

    assert caught.value.code is V12ModelRuntimeFailure.METRICS_INVALID
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


@pytest.mark.asyncio
async def test_q38_factory_selects_sglang_and_accepts_bounded_68k_metrics(settings) -> None:
    router = _router(settings)
    observed: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        if request.url.path == "/_friday/v1/deployment-witness":
            return httpx.Response(200, content=_deployment_witness())
        if request.url.path == "/metrics":
            return httpx.Response(200, content=_metrics_body_68k())
        if request.url.path == "/server_info":
            return httpx.Response(200, content=_server_info())
        raise AssertionError("unexpected endpoint")

    runtime = create_attested_v12_model_runtime(
        router,
        metrics_http_transport=httpx.MockTransport(handler),
    )
    sample = await runtime.probe_client.sample_load(absolute_deadline=time.monotonic() + 2)

    assert runtime.profile is QWEN38_27B_SGLANG_V12_PROFILE
    assert (sample.running, sample.waiting) == (2.0, 3.0)
    assert [str(request.url) for request in observed] == [
        "http://127.0.0.1:8001/_friday/v1/deployment-witness",
        "http://127.0.0.1:8001/metrics",
        "http://127.0.0.1:8001/server_info",
        "http://127.0.0.1:8001/_friday/v1/deployment-witness",
    ]
    assert all(
        request.headers["authorization"] == "Bearer private-v12-key"
        and request.headers["accept-encoding"] == "identity"
        for request in observed
    )


@pytest.mark.asyncio
async def test_q38_runtime_observes_stable_and_drifted_server_epochs(settings) -> None:
    server_seeds = iter((41, 41, 42))
    witness_seeds = iter((41, 41, 41, 41, 42, 42))

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/_friday/v1/deployment-witness":
            return httpx.Response(
                200,
                content=_deployment_witness(random_seed=next(witness_seeds)),
            )
        if request.url.path == "/metrics":
            return httpx.Response(200, content=_sglang_metrics())
        if request.url.path == "/server_info":
            return httpx.Response(
                200,
                content=_server_info(random_seed=next(server_seeds)),
            )
        raise AssertionError("unexpected endpoint")

    runtime = create_attested_v12_model_runtime(
        _router(settings),
        metrics_http_transport=httpx.MockTransport(handler),
    )
    samples = [
        await runtime.probe_client.sample_load(absolute_deadline=time.monotonic() + 2) for _ in range(3)
    ]

    assert samples[0].process_epoch_sha256 == samples[1].process_epoch_sha256
    assert samples[1].process_epoch_sha256 != samples[2].process_epoch_sha256


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["witness-drift", "seed-mismatch"])
async def test_q38_runtime_rejects_torn_or_seed_mismatched_sample(
    settings,
    failure: str,
) -> None:
    witness_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal witness_calls
        if request.url.path == "/_friday/v1/deployment-witness":
            witness_calls += 1
            nonce = "b" * 64 if failure == "witness-drift" and witness_calls == 2 else "a" * 64
            return httpx.Response(200, content=_deployment_witness(nonce=nonce))
        if request.url.path == "/metrics":
            return httpx.Response(200, content=_sglang_metrics())
        if request.url.path == "/server_info":
            seed = 786_846_034 if failure == "seed-mismatch" else 786_846_033
            return httpx.Response(200, content=_server_info(random_seed=seed))
        raise AssertionError("unexpected endpoint")

    runtime = create_attested_v12_model_runtime(
        _router(settings),
        metrics_http_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ModelProbeError) as caught:
        await runtime.probe_client.sample_load(absolute_deadline=time.monotonic() + 2)

    assert caught.value.code is ModelProbeFailure.LOAD_INVALID
    assert witness_calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["server-info", "witness"])
async def test_q38_runtime_rejects_wrong_identity_without_secret_retention(
    settings,
    failure: str,
) -> None:
    secret = "runtime-identity-secret-sentinel"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/_friday/v1/deployment-witness":
            if failure == "witness":
                body = _deployment_witness()[:-1] + f',"extra":"{secret}"}}'.encode()
                return httpx.Response(200, content=body)
            return httpx.Response(200, content=_deployment_witness())
        if request.url.path == "/metrics":
            return httpx.Response(200, content=_sglang_metrics())
        if request.url.path == "/server_info":
            if failure == "server-info":
                return httpx.Response(200, content=_server_info(version=secret, api_key=secret))
            return httpx.Response(200, content=_server_info())
        raise AssertionError("unexpected endpoint")

    runtime = create_attested_v12_model_runtime(
        _router(settings),
        metrics_http_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ModelProbeError) as caught:
        await runtime.probe_client.sample_load(absolute_deadline=time.monotonic() + 2)

    assert caught.value.code is ModelProbeFailure.LOAD_INVALID
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert not _exception_traceback_contains_marker(caught.value, secret)


@pytest.mark.asyncio
async def test_q38_runtime_cancellation_after_server_info_does_not_retain_secret(
    settings,
) -> None:
    secret = "cancelled-server-info-secret-sentinel"
    second_witness_started = asyncio.Event()
    block_second_witness = asyncio.Event()
    witness_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal witness_calls
        if request.url.path == "/_friday/v1/deployment-witness":
            witness_calls += 1
            if witness_calls == 2:
                second_witness_started.set()
                await block_second_witness.wait()
            return httpx.Response(200, content=_deployment_witness())
        if request.url.path == "/metrics":
            return httpx.Response(200, content=_sglang_metrics())
        if request.url.path == "/server_info":
            return httpx.Response(200, content=_server_info(api_key=secret))
        raise AssertionError("unexpected endpoint")

    runtime = create_attested_v12_model_runtime(
        _router(settings),
        metrics_http_transport=httpx.MockTransport(handler),
    )
    task = asyncio.create_task(runtime.probe_client.sample_load(absolute_deadline=time.monotonic() + 2))
    await asyncio.wait_for(second_witness_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert not _exception_traceback_contains_marker(caught.value, secret)


@pytest.mark.asyncio
async def test_vllm_path_keeps_64k_bound_and_does_not_accept_sglang(settings) -> None:
    oversized_vllm = (
        b"process_start_time_seconds 1\n"
        b'vllm:num_requests_running{model_name="dispatcher"} 0\n'
        b'vllm:num_requests_waiting{model_name="dispatcher"} 0\n' + b"# pad\n" * 12_000
    )
    assert len(oversized_vllm) > MAX_VLLM_METRICS_BYTES
    with pytest.raises(V12ModelRuntimeError):
        _parse_metrics(oversized_vllm, served_model_alias="dispatcher")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_metrics_body_68k())

    runtime = create_attested_v12_model_runtime(
        _router(settings, q38=False),
        metrics_http_transport=httpx.MockTransport(handler),
    )
    assert runtime.profile is QWEN36_27B_V12_PROFILE
    with pytest.raises(ModelProbeError) as caught:
        await runtime.probe_client.sample_load(absolute_deadline=time.monotonic() + 2)
    assert caught.value.code is ModelProbeFailure.LOAD_CALL_FAILED


def test_factory_rejects_unregistered_alias_and_copied_runtime_profile(settings) -> None:
    wrong_alias = _router(settings, llm_model="not-dispatcher")
    copied_profile = replace(PROFILES["qwen38-27b-nvfp4-sglang"])
    copied_settings = replace(
        settings,
        profile=copied_profile,
        llm_enabled=True,
        llm_model="dispatcher",
        llm_api_key="private-v12-key",
        llm_base_url="http://127.0.0.1:8001/v1",
    )

    for router in (wrong_alias, LLMRouter(copied_settings)):
        with pytest.raises(V12ModelTransportError) as caught:
            create_attested_v12_model_runtime(router)
        assert caught.value.code is V12ModelTransportFailure.COMPOSITION_REJECTED
        assert "private-v12-key" not in repr(caught.value)


def test_factory_rejects_registered_q38_profile_with_empty_witness_constant(
    settings,
    monkeypatch,
) -> None:
    profile = replace(
        PROFILES["qwen38-27b-nvfp4-sglang"],
        engine_image_id="",
    )
    monkeypatch.setitem(PROFILES, profile.name, profile)
    router = LLMRouter(
        replace(
            settings,
            profile=profile,
            llm_enabled=True,
            llm_model="dispatcher",
            llm_api_key="private-v12-key",
            llm_base_url="http://127.0.0.1:8001/v1",
        )
    )

    with pytest.raises(V12ModelTransportError) as caught:
        create_attested_v12_model_runtime(router)

    assert caught.value.code is V12ModelTransportFailure.COMPOSITION_REJECTED
