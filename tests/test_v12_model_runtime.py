from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

import pytest

import friday.agent_runtime.llm as llm_module
from friday.agent_runtime.llm import LLMRouter
from friday.config import PROFILES
from friday.model_probe import (
    CONTEXT_OUTPUT_RESERVE_TOKENS,
    CONTEXT_PROBE,
    CONTEXT_SAFETY_RESERVE_TOKENS,
    PLAN_PROBE_CASES,
    POST_CONTEXT_IDLE_RETRY_INTERVAL_SEC,
    SYNTHESIS_PROBE,
    CancellationProbeRequest,
    ModelProbeError,
    ModelProbeFailure,
)
from friday.model_profiles import (
    QWEN36_27B_V12_PROFILE,
    QWEN38_27B_SGLANG_V12_PROFILE,
    ModelCapability,
    ModelEffect,
    ModelRequirements,
    V12LiveAttestation,
)
from friday.v12_model_runtime import (
    MAX_METRICS_BYTES,
    MAX_MODEL_INVENTORY_BYTES,
    AttestedV12ModelRuntime,
    V12ModelRuntimeError,
    V12ModelRuntimeFailure,
    V12ServedCompletion,
    _derive_endpoint_binding,
    _parse_metrics,
    _parse_model_inventory,
)

_EPOCH = "1700000000.00000001"
_EPOCH_SHA256 = hashlib.sha256(b"1700000000.00000001").hexdigest()
_OTHER_EPOCH = "1700000001"


def _metrics(
    *,
    epoch: str = _EPOCH,
    running: str = "0.0",
    waiting: str = "0",
    labels: bool = True,
) -> bytes:
    suffix = '{model_name="dispatcher"}' if labels else ""
    return (
        "# HELP process_start_time_seconds Start time.\n"
        f"process_start_time_seconds {epoch}\n"
        f"vllm:num_requests_running{suffix} {running}\n"
        f"vllm:num_requests_waiting{suffix} {waiting}\n"
    ).encode()


def _plan_payload(case_id: str, route: str, *, output_format: str = "text") -> str:
    evidence: list[dict[str, object]] = []
    if route == "file_read":
        evidence = [{"kind": "attached_files", "query": "", "max_items": 2, "required": True}]
    elif route == "archive_read":
        evidence = [{"kind": "archive", "query": "probe", "max_items": 2, "required": True}]
    elif route == "web_read":
        evidence = [{"kind": "web", "query": "probe", "max_items": 2, "required": True}]
    return json.dumps(
        {
            "schema": "friday.turn-plan.v1",
            "route": route,
            "objective": "synthetic objective",
            "evidence_requests": evidence,
            "tool_intents": [],
            "output": {
                "format": output_format,
                "language": "ru",
                "require_citations": route in {"file_read", "archive_read", "web_read"},
                "one_message": True,
            },
            "confidence": 0.9,
            "fallback": "legacy",
            "reason_code": case_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


class _MetricsTransport:
    def __init__(self, router: LLMRouter, bodies: list[bytes] | None = None) -> None:
        self._router = router
        self.bodies = list(bodies or [_metrics()])
        self.calls: list[tuple[int, float]] = []
        self.call_times: list[float] = []
        self.returned_bodies: list[bytes] = []

    @property
    def bound_router(self) -> LLMRouter:
        return self._router

    async def fetch_metrics(self, *, maximum_bytes: int, absolute_deadline: float) -> bytes:
        self.calls.append((maximum_bytes, absolute_deadline))
        self.call_times.append(time.monotonic())
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        self.returned_bodies.append(body)
        return body

    async def fetch_model_inventory(
        self,
        *,
        maximum_bytes: int,
        absolute_deadline: float,
    ) -> bytes:
        assert maximum_bytes > 0
        assert absolute_deadline > time.monotonic()
        return b'{"object":"list","data":[{"id":"dispatcher","object":"model"}]}'

    async def fetch_server_info(
        self,
        *,
        maximum_bytes: int,
        absolute_deadline: float,
    ) -> bytes:
        assert maximum_bytes > 0
        assert absolute_deadline > time.monotonic()
        return b"{}"

    async def fetch_deployment_witness(
        self,
        *,
        maximum_bytes: int,
        absolute_deadline: float,
    ) -> bytes:
        assert maximum_bytes > 0
        assert absolute_deadline > time.monotonic()
        return b"{}"


class _Pending:
    def __init__(self, router: LLMRouter, *, alias: str = "dispatcher") -> None:
        self._router = router
        self._alias = alias
        self.pending = True
        self.submitted = True
        self.cancel_calls = 0

    @property
    def bound_router(self) -> LLMRouter:
        return self._router

    @property
    def submitted_model_alias(self) -> str:
        return self._alias

    def is_pending(self) -> bool:
        return self.pending

    def submission_started(self) -> bool:
        return self.submitted

    async def cancel_and_drain(self, *, absolute_deadline: float) -> bool:
        assert absolute_deadline > 0
        self.cancel_calls += 1
        self.pending = False
        return True


class _CompletionTransport:
    def __init__(
        self,
        router: LLMRouter,
        responses: list[V12ServedCompletion | BaseException] | None = None,
    ) -> None:
        self._router = router
        self.responses = list(
            responses
            or [
                V12ServedCompletion(
                    content="clear",
                    finish_reason="stop",
                    tool_calls=(),
                    prompt_tokens=32,
                    served_model_alias="dispatcher",
                )
            ]
        )
        self.calls: list[dict[str, Any]] = []
        self.pending = _Pending(router)

    @property
    def bound_router(self) -> LLMRouter:
        return self._router

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None,
        max_tokens: int | None,
        priority: str,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
        reject_repeated_token_degeneration: bool,
        allow_retries: bool,
        absolute_deadline: float,
        open_silent_cooldown: bool,
        require_full_context: bool,
    ) -> V12ServedCompletion:
        self.calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "priority": priority,
                "tools": tools,
                "tool_choice": tool_choice,
                "reject": reject_repeated_token_degeneration,
                "retries": allow_retries,
                "deadline": absolute_deadline,
                "cooldown": open_silent_cooldown,
                "full": require_full_context,
            }
        )
        value = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(value, BaseException):
            raise value
        return value

    async def start_cancellable(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None,
        max_tokens: int | None,
        priority: str,
        absolute_deadline: float,
        require_full_context: bool,
    ) -> _Pending:
        self.calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "priority": priority,
                "deadline": absolute_deadline,
                "full": require_full_context,
                "cancellable": True,
            }
        )
        return self.pending


def _router(settings, **changes: Any) -> LLMRouter:
    values = {
        "llm_enabled": True,
        "llm_model": "dispatcher",
        "llm_api_key": "private-runtime-key",
        **changes,
    }
    configured = replace(settings, **values)
    return LLMRouter(configured)


def _runtime(
    settings,
    *,
    metrics: list[bytes] | None = None,
    completions: list[V12ServedCompletion | BaseException] | None = None,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[AttestedV12ModelRuntime, _CompletionTransport, _MetricsTransport]:
    router = _router(settings)
    completion = _CompletionTransport(router, completions)
    metric_transport = _MetricsTransport(router, metrics)
    runtime = AttestedV12ModelRuntime(
        router,
        completion,
        metric_transport,
        sleeper=sleeper,
    )
    return runtime, completion, metric_transport


def _requirements() -> ModelRequirements:
    return ModelRequirements(
        capabilities=QWEN36_27B_V12_PROFILE.required_capabilities,
        required_context_tokens=8_192,
        prepared_evidence_items=2,
        max_tool_steps=0,
        effect=ModelEffect.READ,
        verifier_required=True,
    )


def _install_attestation(runtime: AttestedV12ModelRuntime, *, epoch: str = _EPOCH_SHA256) -> None:
    profile = runtime.profile
    attestation = V12LiveAttestation(
        profile_id=profile.profile_id,
        planner_contract_sha256=profile.planner_contract_sha256,
        probe_suite_sha256=profile.probe_suite_sha256,
        endpoint_binding_sha256=runtime._seal.endpoint_binding_sha256,  # noqa: SLF001
        process_epoch_sha256=epoch,
        capabilities=profile.required_capabilities,
        verified_context_tokens=profile.minimum_context_tokens,
        max_prepared_evidence_items=profile.max_prepared_evidence_items,
        max_tool_steps=profile.max_tool_steps,
        max_tool_rounds=profile.max_tool_rounds,
        max_tool_calls=profile.max_tool_calls,
        allowed_effects=profile.allowed_effects,
        verifier_required=True,
    )
    assert runtime._gate.install_live(attestation) is True  # noqa: SLF001


def test_metrics_parser_decimal_normalizes_epoch_and_requires_exact_gauges() -> None:
    first = _parse_metrics(_metrics(epoch="1700000000.0000000100"), served_model_alias="dispatcher")
    second = _parse_metrics(_metrics(epoch="1700000000.00000001"), served_model_alias="dispatcher")

    assert first == second
    assert first.process_epoch_sha256 == _EPOCH_SHA256
    assert first.running == first.waiting == 0.0
    assert (
        _parse_metrics(
            _metrics(running="2.0", waiting="3", labels=False),
            served_model_alias="dispatcher",
        ).running
        == 2.0
    )
    assert (
        _parse_metrics(
            _metrics().replace(
                b'{model_name="dispatcher"}',
                b'{engine="0",model_name="dispatcher"}',
            ),
            served_model_alias="dispatcher",
        ).waiting
        == 0.0
    )


def test_model_inventory_requires_one_exact_served_alias() -> None:
    _parse_model_inventory(
        b'{"object":"list","data":[{"id":"dispatcher","object":"model"}]}',
        served_model_alias="dispatcher",
    )


@pytest.mark.parametrize(
    "body",
    [
        b'{"object":"list","data":[]}',
        b'{"object":"list","data":[{"id":"other","object":"model"}]}',
        b'{"object":"list","data":[{"id":"dispatcher","object":"model"},{"id":"other","object":"model"}]}',
        b'{"object":"list","object":"list","data":[]}',
        b'{"object":"list","data":NaN}',
        b"x" * (MAX_MODEL_INVENTORY_BYTES + 1),
    ],
)
def test_model_inventory_rejects_ambiguous_or_untrusted_identity(body: bytes) -> None:
    with pytest.raises(V12ModelRuntimeError) as caught:
        _parse_model_inventory(body, served_model_alias="dispatcher")

    assert caught.value.code is V12ModelRuntimeFailure.SERVED_ALIAS_REJECTED


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"process_start_time_seconds 1\n",
        _metrics().replace(b"vllm:num_requests_running", b"vllm_num_requests_running"),
        _metrics().replace(b'model_name="dispatcher"', b'model_name="other"'),
        _metrics(running="0.5"),
        _metrics(waiting="NaN"),
        _metrics(epoch="0"),
        _metrics() + b"vllm:num_requests_waiting 0\n",
        b"x" * (MAX_METRICS_BYTES + 1),
    ],
)
def test_metrics_parser_fails_closed_on_missing_ambiguous_or_non_exact_samples(body: bytes) -> None:
    with pytest.raises(V12ModelRuntimeError) as caught:
        _parse_metrics(body, served_model_alias="dispatcher")

    assert caught.value.code is V12ModelRuntimeFailure.METRICS_INVALID


def test_runtime_requires_exact_router_profile_and_same_bound_transports(settings) -> None:
    router = _router(settings)
    other = _router(settings)

    with pytest.raises(V12ModelRuntimeError, match="composition_rejected"):
        AttestedV12ModelRuntime(
            router,
            _CompletionTransport(other),
            _MetricsTransport(router),
        )

    class _RouterSubclass(LLMRouter):
        pass

    with pytest.raises(V12ModelRuntimeError, match="composition_rejected"):
        AttestedV12ModelRuntime(
            _RouterSubclass(router.settings),
            _CompletionTransport(router),
            _MetricsTransport(router),
        )

    replaced_profile = replace(QWEN36_27B_V12_PROFILE)
    with pytest.raises(V12ModelRuntimeError, match="composition_rejected"):
        AttestedV12ModelRuntime(
            router,
            _CompletionTransport(router),
            _MetricsTransport(router),
            profile=replaced_profile,
        )


@pytest.mark.parametrize("invalid_cap", [True, 0, 1 << 63], ids=("bool", "zero", "overflow"))
def test_runtime_rejects_malformed_installation_context_caps(
    settings,
    monkeypatch,
    invalid_cap: object,
) -> None:
    profile = QWEN36_27B_V12_PROFILE
    installed = replace(
        PROFILES[profile.runtime_profile_name],
        max_model_len=invalid_cap,  # type: ignore[arg-type]
    )
    monkeypatch.setitem(PROFILES, profile.runtime_profile_name, installed)
    router = _router(settings, profile=installed)

    with pytest.raises(V12ModelRuntimeError) as caught:
        AttestedV12ModelRuntime(
            router,
            _CompletionTransport(router),
            _MetricsTransport(router),
            profile=profile,
        )

    assert caught.value.code is V12ModelRuntimeFailure.COMPOSITION_REJECTED


@pytest.mark.parametrize(
    "invalid_cap",
    [True, 0, float("nan"), 1 << 63],
    ids=("bool", "zero", "nonfinite", "overflow"),
)
def test_runtime_rejects_malformed_sglang_total_token_caps(
    settings,
    monkeypatch,
    invalid_cap: object,
) -> None:
    profile = QWEN38_27B_SGLANG_V12_PROFILE
    installed = PROFILES[profile.runtime_profile_name]
    launch = installed.sglang_extra_args
    assert launch is not None
    installed = replace(
        installed,
        sglang_extra_args=replace(
            launch,
            max_total_tokens=invalid_cap,  # type: ignore[arg-type]
        ),
    )
    monkeypatch.setitem(PROFILES, profile.runtime_profile_name, installed)
    router = _router(settings, profile=installed)

    with pytest.raises(V12ModelRuntimeError) as caught:
        AttestedV12ModelRuntime(
            router,
            _CompletionTransport(router),
            _MetricsTransport(router),
            profile=profile,
        )

    assert caught.value.code in {
        V12ModelRuntimeFailure.COMPOSITION_REJECTED,
        V12ModelRuntimeFailure.SETTINGS_REJECTED,
    }


def test_private_endpoint_binding_covers_auth_settings_and_never_appears_in_repr(settings) -> None:
    first = _router(settings)
    same = _router(settings)
    changed = _router(settings, llm_api_key="another-private-key")
    changed_installation = _router(
        settings,
        profile=replace(settings.profile, max_model_len=settings.profile.max_model_len - 1),
    )

    first_binding = _derive_endpoint_binding(first, QWEN36_27B_V12_PROFILE)
    same_binding = _derive_endpoint_binding(same, QWEN36_27B_V12_PROFILE)
    changed_binding = _derive_endpoint_binding(changed, QWEN36_27B_V12_PROFILE)
    changed_installation_binding = _derive_endpoint_binding(
        changed_installation,
        QWEN36_27B_V12_PROFILE,
    )

    assert first_binding == same_binding
    assert first_binding != changed_binding
    assert first_binding != changed_installation_binding
    runtime = AttestedV12ModelRuntime(
        first,
        _CompletionTransport(first),
        _MetricsTransport(first),
    )
    rendered = repr(runtime) + repr(runtime._seal)  # noqa: SLF001
    assert "private-runtime-key" not in rendered
    assert first_binding not in rendered


def test_measured_probe_reserves_match_the_exact_router_admission_contract() -> None:
    assert getattr(llm_module, "_CONTEXT_SAFETY_TOKENS", None) == CONTEXT_SAFETY_RESERVE_TOKENS


@pytest.mark.parametrize(
    "limiting_cap",
    ["max_model_len", "max_total_tokens"],
    ids=("runtime-profile", "sglang-launch"),
)
def test_runtime_threads_the_strictest_installation_cap_into_the_measured_gate(
    settings,
    monkeypatch,
    limiting_cap: str,
) -> None:
    profile = QWEN38_27B_SGLANG_V12_PROFILE
    installed = PROFILES[profile.runtime_profile_name]
    launch = installed.sglang_extra_args
    assert launch is not None
    if limiting_cap == "max_model_len":
        installed = replace(installed, max_model_len=32_768)
    else:
        installed = replace(
            installed,
            sglang_extra_args=replace(launch, max_total_tokens=32_768),
        )
    monkeypatch.setitem(PROFILES, profile.runtime_profile_name, installed)
    router = LLMRouter(
        replace(
            settings,
            profile=installed,
            llm_enabled=True,
            llm_model=profile.served_model_alias,
            llm_api_key="private-runtime-key",
        )
    )
    runtime = AttestedV12ModelRuntime(
        router,
        _CompletionTransport(router),
        _MetricsTransport(router),
        profile=profile,
    )
    attestation = V12LiveAttestation(
        profile_id=profile.profile_id,
        planner_contract_sha256=profile.planner_contract_sha256,
        probe_suite_sha256=profile.probe_suite_sha256,
        endpoint_binding_sha256=runtime._seal.endpoint_binding_sha256,  # noqa: SLF001
        process_epoch_sha256=_EPOCH_SHA256,
        capabilities=profile.required_capabilities,
        verified_context_tokens=40_960,
        max_prepared_evidence_items=profile.max_prepared_evidence_items,
        max_tool_steps=profile.max_tool_steps,
        max_tool_rounds=profile.max_tool_rounds,
        max_tool_calls=profile.max_tool_calls,
        allowed_effects=profile.allowed_effects,
        verifier_required=profile.verifier_required,
    )
    assert runtime._gate.install_live(attestation) is True  # noqa: SLF001

    requirements = ModelRequirements(
        capabilities=profile.required_capabilities,
        required_context_tokens=32_768,
        prepared_evidence_items=profile.max_prepared_evidence_items,
        max_tool_steps=profile.max_tool_steps,
        max_tool_rounds=profile.max_tool_rounds,
        max_tool_calls=profile.max_tool_calls,
        effect=ModelEffect.READ,
        verifier_required=True,
    )
    assert (
        runtime._gate.lease(  # noqa: SLF001
            requirements,
            process_epoch_sha256=_EPOCH_SHA256,
        )
        is not None
    )
    assert (
        runtime._gate.lease(  # noqa: SLF001
            replace(requirements, required_context_tokens=32_769),
            process_epoch_sha256=_EPOCH_SHA256,
        )
        is None
    )
    assert runtime.public_status()["installation_context_tokens"] == 32_768
    assert runtime.public_status()["verified_context_tokens"] == 40_960
    assert runtime.public_status()["effective_context_tokens"] == 32_768


@pytest.mark.asyncio
async def test_probe_client_uses_real_planner_and_rejects_a_changed_served_alias(settings) -> None:
    case = PLAN_PROBE_CASES[0]
    runtime, completion, _metrics_transport = _runtime(
        settings,
        completions=[
            V12ServedCompletion(
                content=_plan_payload(case.case_id, case.expected_route.value),
                finish_reason="stop",
                tool_calls=(),
                prompt_tokens=321,
                served_model_alias="dispatcher",
            ),
            V12ServedCompletion(
                content="{}",
                finish_reason="stop",
                tool_calls=(),
                prompt_tokens=2,
                served_model_alias="different",
            ),
        ],
    )

    result = await runtime.probe_client.complete_plan(
        case,
        absolute_deadline=time.monotonic() + 2,
    )

    assert json.loads(result.content)["route"] == case.expected_route.value
    assert result.prompt_tokens == 321
    assert completion.calls[0]["full"] is True
    assert completion.calls[0]["retries"] is False
    assert completion.calls[0]["tools"] is None

    with pytest.raises(ModelProbeError) as caught:
        await runtime.probe_client.complete_synthesis(
            replace(SYNTHESIS_PROBE, prompt="synthetic"),
            absolute_deadline=time.monotonic() + 2,
        )
    assert caught.value.code is ModelProbeFailure.SYNTHESIS_CALL_FAILED


@pytest.mark.asyncio
async def test_runtime_can_install_only_the_complete_live_probe_result(settings) -> None:
    plan_responses = [
        V12ServedCompletion(
            content=_plan_payload(
                case.case_id,
                case.expected_route.value,
                output_format=(case.expected_output_format.value if case.expected_output_format else "text"),
            ),
            finish_reason="stop",
            tool_calls=(),
            prompt_tokens=512,
            served_model_alias="dispatcher",
        )
        for case in PLAN_PROBE_CASES
    ]
    synthesis_responses = [
        V12ServedCompletion(
            content="Код синтетического проекта: СЕВЕР-42 [A1].",
            finish_reason="stop",
            tool_calls=(),
            prompt_tokens=256,
            served_model_alias="dispatcher",
        ),
        V12ServedCompletion(
            content=(
                "Код синтетического проекта: СЕВЕР-42 [A1]. "
                "Контрольная дата синтетического проекта: 7 октября 2099 года [A2]."
            ),
            finish_reason="stop",
            tool_calls=(),
            prompt_tokens=256,
            served_model_alias="dispatcher",
        ),
    ]
    positive_verifier_responses = [
        V12ServedCompletion(
            content=json.dumps(
                {
                    "schema": "friday.v12-file-verifier.v1",
                    "supported": True,
                    "citation_labels": ["A1"] if index == 0 else ["A1", "A2"],
                    "unsupported_claims": 0,
                },
                separators=(",", ":"),
            ),
            finish_reason="stop",
            tool_calls=(),
            prompt_tokens=256,
            served_model_alias="dispatcher",
        )
        for index in range(2)
    ]
    negative_verifier = V12ServedCompletion(
        content=(
            '{"schema":"friday.v12-file-verifier.v1","supported":false,'
            '"citation_labels":["A1","A2"],"unsupported_claims":1}'
        ),
        finish_reason="stop",
        tool_calls=(),
        prompt_tokens=256,
        served_model_alias="dispatcher",
    )
    context = V12ServedCompletion(
        content=json.dumps(
            {"начало": CONTEXT_PROBE.start_marker, "конец": CONTEXT_PROBE.end_marker},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        finish_reason="stop",
        tool_calls=(),
        prompt_tokens=8_192,
        served_model_alias="dispatcher",
    )
    runtime, completion, metrics = _runtime(
        settings,
        metrics=[
            _metrics(),
            _metrics(running="1"),
            _metrics(),
            _metrics(),
            _metrics(running="1"),
            _metrics(running="1"),
            _metrics(),
            _metrics(),
            _metrics(),
            _metrics(),
        ],
        completions=[
            *plan_responses,
            synthesis_responses[0],
            positive_verifier_responses[0],
            synthesis_responses[1],
            positive_verifier_responses[1],
            negative_verifier,
            context,
        ],
    )

    attestation = await runtime.attest(absolute_deadline=time.monotonic() + 5)

    assert attestation.profile_id == QWEN36_27B_V12_PROFILE.profile_id
    assert attestation.process_epoch_sha256 == _EPOCH_SHA256
    assert runtime.public_status()["status"] == "canary_ready"
    assert len(completion.calls) == len(PLAN_PROBE_CASES) + 7
    assert [
        call["max_tokens"]
        for call in completion.calls
        if call["messages"] == [{"role": "user", "content": CONTEXT_PROBE.prompt}]
    ] == [CONTEXT_OUTPUT_RESERVE_TOKENS]
    assert completion.pending.cancel_calls == 1
    assert [
        (_parse_metrics(body, served_model_alias="dispatcher").running) for body in metrics.returned_bodies
    ] == [0.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0]
    assert len(metrics.calls) == 10
    assert metrics.call_times[2] - metrics.call_times[1] >= POST_CONTEXT_IDLE_RETRY_INTERVAL_SEC * 0.9


async def _no_sleep(_delay: float) -> None:
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cancellation_requires_idle_to_positive_pending_then_local_and_stable_remote_drain(
    settings,
) -> None:
    runtime, completion, metrics = _runtime(
        settings,
        metrics=[
            _metrics(),
            _metrics(running="1"),
            _metrics(running="1"),
            _metrics(),
            _metrics(),
        ],
        sleeper=_no_sleep,
    )
    request = CancellationProbeRequest(
        case_id="cancel-contract",
        prompt="synthetic pending generation",
        cancel_after_ms=0,
        queue_drain_timeout_ms=1_000,
    )

    result = await runtime.probe_client.cancel_and_drain(
        request,
        absolute_deadline=time.monotonic() + 2,
    )

    assert result.phase == "submitted"
    assert result.local_task_drained is True
    assert completion.pending.cancel_calls == 1
    assert completion.pending.pending is False
    assert len(metrics.calls) == 5


@pytest.mark.asyncio
async def test_cancellation_rejects_completion_without_positive_server_load(settings) -> None:
    runtime, completion, _ = _runtime(
        settings,
        metrics=[_metrics(), _metrics()],
        sleeper=_no_sleep,
    )

    async def finish_on_sleep(_delay: float) -> None:
        completion.pending.pending = False
        await asyncio.sleep(0)

    runtime._client._sleep = finish_on_sleep  # noqa: SLF001
    request = CancellationProbeRequest("not-accepted", "synthetic", 0, 1_000)

    with pytest.raises(ModelProbeError) as caught:
        await runtime.probe_client.cancel_and_drain(
            request,
            absolute_deadline=time.monotonic() + 2,
        )

    assert caught.value.code is ModelProbeFailure.CANCELLATION_INVALID
    assert completion.pending.cancel_calls == 0


@pytest.mark.asyncio
async def test_cancellation_does_not_attribute_external_load_before_its_http_send(
    settings,
    monkeypatch,
) -> None:
    runtime, completion, metrics = _runtime(
        settings,
        metrics=[_metrics(), _metrics(running="1")],
    )
    completion.pending.submitted = False
    monkeypatch.setattr("friday.v12_model_runtime.CANCELLATION_TIMEOUT_SEC", 0.03)
    request = CancellationProbeRequest("not-submitted", "synthetic", 0, 1_000)

    with pytest.raises(ModelProbeError) as caught:
        await runtime.probe_client.cancel_and_drain(
            request,
            absolute_deadline=time.monotonic() + 1,
        )

    assert caught.value.code is ModelProbeFailure.CANCELLATION_INVALID
    assert len(metrics.calls) == 1
    assert completion.pending.cancel_calls == 1


@pytest.mark.asyncio
async def test_acquire_validate_and_complete_recheck_epoch_without_auto_reacquire(settings) -> None:
    runtime, completion, metrics = _runtime(settings)
    _install_attestation(runtime)
    requirements = _requirements()
    deadline = time.monotonic() + 3

    lease = await runtime.acquire_lease(requirements, absolute_deadline=deadline)
    assert lease is not None
    assert await runtime.lease_is_current(
        lease,
        requirements,
        absolute_deadline=deadline,
    )

    result = await runtime.complete(
        lease,
        requirements,
        [{"role": "user", "content": "synthetic"}],
        max_tokens=128,
        priority="foreground",
        absolute_deadline=deadline,
    )

    assert result["content"] == "clear"
    assert result["tool_calls"] == []
    assert completion.calls[-1]["full"] is True
    assert completion.calls[-1]["retries"] is False
    assert len(metrics.calls) == 4  # acquire, validate, pre-call, post-call

    runtime._gate.revoke()  # noqa: SLF001
    assert await runtime.acquire(requirements, absolute_deadline=deadline) is None
    with pytest.raises(V12ModelRuntimeError) as caught:
        await runtime.checked_chat(
            lease,
            requirements,
            [{"role": "user", "content": "do not reacquire"}],
            max_tokens=128,
            absolute_deadline=deadline,
        )
    assert caught.value.code is V12ModelRuntimeFailure.LEASE_REJECTED


@pytest.mark.asyncio
async def test_checked_completion_cannot_exceed_the_exact_leased_context(settings) -> None:
    runtime, completion, metrics = _runtime(settings)
    _install_attestation(runtime)
    messages = [{"role": "user", "content": "synthetic"}]
    max_tokens = 300
    required_context_tokens = (
        runtime._seal.router.estimate_messages_tokens(messages)  # noqa: SLF001
        + max_tokens
        + CONTEXT_SAFETY_RESERVE_TOKENS
        - 1
    )
    requirements = replace(
        _requirements(),
        required_context_tokens=required_context_tokens,
    )
    lease = runtime._gate.lease(  # noqa: SLF001
        requirements,
        process_epoch_sha256=_EPOCH_SHA256,
    )
    assert lease is not None

    with pytest.raises(V12ModelRuntimeError) as caught:
        await runtime.complete(
            lease,
            requirements,
            messages,
            max_tokens=max_tokens,
            priority="foreground",
            absolute_deadline=time.monotonic() + 2,
        )

    assert caught.value.code is V12ModelRuntimeFailure.COMPLETION_INVALID
    assert completion.calls == []
    assert len(metrics.calls) == 1


@pytest.mark.asyncio
async def test_checked_completion_revokes_on_epoch_drift_even_after_a_model_response(settings) -> None:
    runtime, _completion, _metrics_transport = _runtime(
        settings,
        metrics=[_metrics(epoch=_EPOCH), _metrics(epoch=_OTHER_EPOCH)],
    )
    _install_attestation(runtime)
    requirements = _requirements()
    lease = runtime._gate.lease(  # noqa: SLF001
        requirements,
        process_epoch_sha256=_EPOCH_SHA256,
    )
    assert lease is not None

    with pytest.raises(V12ModelRuntimeError) as caught:
        await runtime.checked_chat(
            lease,
            requirements,
            [{"role": "user", "content": "synthetic"}],
            max_tokens=128,
            absolute_deadline=time.monotonic() + 2,
        )

    assert caught.value.code is V12ModelRuntimeFailure.EPOCH_CHANGED
    assert runtime.public_status()["status"] == "revoked"


@pytest.mark.asyncio
async def test_checked_completion_rejects_alias_drift_and_sanitizes_private_failures(settings) -> None:
    private = "raw-private-model-output-and-url"
    runtime, _completion, metrics = _runtime(
        settings,
        metrics=[_metrics(), _metrics()],
        completions=[
            V12ServedCompletion(
                content=private,
                finish_reason="stop",
                tool_calls=(),
                prompt_tokens=1,
                served_model_alias="wrong-alias",
            )
        ],
    )
    _install_attestation(runtime)
    requirements = _requirements()
    lease = runtime._gate.lease(requirements, process_epoch_sha256=_EPOCH_SHA256)  # noqa: SLF001
    assert lease is not None

    with pytest.raises(V12ModelRuntimeError) as caught:
        await runtime.checked_chat(
            lease,
            requirements,
            [{"role": "user", "content": "synthetic"}],
            max_tokens=128,
            absolute_deadline=time.monotonic() + 2,
        )

    assert caught.value.code is V12ModelRuntimeFailure.SERVED_ALIAS_REJECTED
    assert private not in repr(caught.value)
    assert private not in str(caught.value)
    assert len(metrics.calls) == 2
    assert runtime.public_status()["status"] == "revoked"


@pytest.mark.asyncio
async def test_checked_completion_propagates_cancellation_without_a_post_call_metrics_probe(
    settings,
) -> None:
    runtime, _completion, metrics = _runtime(
        settings,
        metrics=[_metrics()],
        completions=[asyncio.CancelledError()],
    )
    _install_attestation(runtime)
    requirements = _requirements()
    lease = runtime._gate.lease(requirements, process_epoch_sha256=_EPOCH_SHA256)  # noqa: SLF001
    assert lease is not None

    with pytest.raises(asyncio.CancelledError):
        await runtime.checked_chat(
            lease,
            requirements,
            [{"role": "user", "content": "synthetic"}],
            max_tokens=128,
            absolute_deadline=time.monotonic() + 2,
        )

    assert len(metrics.calls) == 1
    assert runtime.public_status()["status"] == "canary_ready"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,max_tokens",
    [
        ("jrc_DO_NOT_FORWARD_THIS_RUNTIME_CREDENTIAL_1234567890", 128),
        ("X" * 6_000, 128),
        ("synthetic", 8_000),
    ],
    ids=("secret", "oversized-input", "oversized-output"),
)
async def test_checked_completion_centrally_rejects_unsafe_or_unattested_payloads(
    settings,
    content: str,
    max_tokens: int,
) -> None:
    runtime, completion, metrics = _runtime(settings)
    _install_attestation(runtime)
    requirements = _requirements()
    lease = runtime._gate.lease(requirements, process_epoch_sha256=_EPOCH_SHA256)  # noqa: SLF001
    assert lease is not None

    with pytest.raises(V12ModelRuntimeError) as caught:
        await runtime.complete(
            lease,
            requirements,
            [{"role": "user", "content": content}],
            max_tokens=max_tokens,
            priority="foreground",
            absolute_deadline=time.monotonic() + 2,
        )

    assert caught.value.code is V12ModelRuntimeFailure.COMPLETION_INVALID
    assert completion.calls == []
    assert len(metrics.calls) == 1


def test_profile_remains_read_only_and_has_no_native_tool_or_vision_authority() -> None:
    assert QWEN36_27B_V12_PROFILE.allowed_effects == frozenset({ModelEffect.READ})
    assert ModelCapability.NATIVE_TOOL_CALLS not in QWEN36_27B_V12_PROFILE.allowed_capabilities
    assert ModelCapability.RAW_VISION not in QWEN36_27B_V12_PROFILE.allowed_capabilities
