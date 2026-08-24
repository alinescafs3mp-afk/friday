from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any

import httpx
import pytest

from friday.config import load_settings
from friday.secondary_brain import (
    EffectClass,
    ModelModality,
    ModelRequest,
    ModelWorkload,
    SecondaryEndpointClient,
    SecondaryEndpointConfig,
    SecondaryFailure,
    SecondaryResult,
    SecondaryState,
    build_secondary_brain,
)

_ALIAS = "friday-secondary-gptoss20b"


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
        base_url="http://secondary.invalid:30000/v1",
        served_model_alias=_ALIAS,
        api_key="synthetic-test-token",
        connect_timeout_sec=0.1,
        read_timeout_sec=0.5,
        call_budget_sec=1.0,
        admission_timeout_sec=0.01,
        cooldown_sec=cooldown_sec,
        max_context_tokens=4096,
        max_concurrency=1,
    )


def _response(
    *,
    model: str = _ALIAS,
    content: str = "accepted",
    message_extra: dict[str, Any] | None = None,
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    message.update(message_extra or {})
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
    )


def _configured_settings(settings: Any, *, mode: str = "assist", private: bool = False) -> Any:
    return replace(
        settings,
        secondary_llm_enabled=True,
        secondary_llm_mode=mode,
        secondary_llm_base_url="http://secondary.invalid:30000/v1",
        secondary_llm_model=_ALIAS,
        secondary_llm_api_key="synthetic-test-token",
        secondary_llm_max_context_tokens=4096,
        secondary_llm_max_concurrency=1,
        secondary_llm_workloads=("classify", "critique"),
        secondary_llm_allow_private_text=private,
    )


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
    assert loaded.secondary_llm_allow_private_text is False

    monkeypatch.setenv("FRIDAY_SECONDARY_LLM_MODE", "typo-that-must-not-assist")
    assert load_settings().secondary_llm_mode == "disabled"


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
    shadow = await scheduler.run_shadow(_request(), primary)

    assert value == "primary"
    assert optional is None
    assert shadow == "primary"
    assert primary_calls == 2
    assert constructed == 0
    assert scheduler.client is None
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

    assert scheduler.client is None
    assert scheduler.status().state is SecondaryState.MISCONFIGURED


@pytest.mark.asyncio
async def test_exact_alias_and_reasoning_are_sanitized(settings: Any) -> None:
    synthetic_reasoning = "internal-" + "scratchpad"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer synthetic-test-token"
        payload = __import__("json").loads(request.content)
        assert payload["model"] == _ALIAS
        assert "tools" not in payload
        return _response(message_extra={"reasoning_content": synthetic_reasoning})

    scheduler = build_secondary_brain(
        _configured_settings(settings),
        transport=httpx.MockTransport(handler),
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
        transport=httpx.MockTransport(handler),
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

    broken_client = SecondaryEndpointClient(
        _endpoint_config(), transport=httpx.MockTransport(broken)
    )
    healthy_client = SecondaryEndpointClient(
        _endpoint_config(), transport=httpx.MockTransport(healthy)
    )
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
        transport=httpx.MockTransport(handler),
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

        result = await scheduler.run_shadow(_request(), same_primary)
        assert result is primary_value
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
        transport=httpx.MockTransport(handler),
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
