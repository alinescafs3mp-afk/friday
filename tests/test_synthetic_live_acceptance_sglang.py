"""The live harness must preserve the product's SGLang restart boundary."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from test_synthetic_live_acceptance import (
    _expected_readiness_classifier_response,
    _readiness_environment,
    acceptance,
    battery,
)
from test_v12_sglang_adapter import _deployment_witness, _server_info, _sglang_metrics

_WITNESS = "/_friday/v1/deployment-witness"
_SAMPLE_PATHS = [_WITNESS, "/metrics", "/server_info", _WITNESS]


def _environment() -> dict[str, str]:
    return {
        **_readiness_environment(),
        "FRIDAY_PROFILE": "qwen38-27b-nvfp4-sglang",
        "JERICHO_PROFILE": "qwen36-27b-nvfp4-nvidia",
    }


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, failure: str = "") -> None:
        self.failure = failure
        self.paths: list[str] = []
        self.witness_count = 0
        self.posts = 0
        self.active = 0
        self.maximum_active = 0
        self.release = asyncio.Event()
        self.metadata_entered = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1" and request.url.port == 8001
        assert request.headers["authorization"] == "Bearer synthetic-readiness-key"
        path = request.url.path
        self.paths.append(path)
        if request.method == "POST":
            assert path == "/v1/chat/completions"
            self.posts += 1
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            if self.active == 4:
                self.release.set()
            try:
                await asyncio.wait_for(self.release.wait(), timeout=1)
                return _expected_readiness_classifier_response(request)
            finally:
                self.active -= 1
        assert request.method == "GET"
        if path == _WITNESS:
            self.witness_count += 1
            if self.failure == "missing-witness":
                return httpx.Response(404)
            if self.failure == "oversized-witness":
                return httpx.Response(200, content=b" " * 8193)
            if self.failure == "metadata-timeout":
                self.metadata_entered.set()
                await asyncio.Event().wait()
            changed = (
                (self.failure == "mid-sample-restart" and self.witness_count == 2)
                or (self.failure == "before-probe-restart" and self.witness_count >= 3)
                or (self.failure == "after-probe-restart" and self.witness_count >= 5)
            )
            return httpx.Response(200, content=_deployment_witness(nonce=("b" if changed else "a") * 64))
        if path == "/metrics":
            busy = self.failure == "busy-before" or (self.failure == "busy-after" and self.posts == 4)
            return httpx.Response(200, content=_sglang_metrics(waiting="1" if busy else "0"))
        assert path == "/server_info"
        if self.failure == "seed-mismatch":
            return httpx.Response(200, content=_server_info(random_seed=1))
        if self.failure == "wrong-runtime":
            return httpx.Response(200, content=_server_info(version="unverified-build"))
        return httpx.Response(200, content=_server_info())


def test_sglang_readiness_preserves_idle_intervals_and_four_way_wave() -> None:
    transport = _Transport()
    quiet: list[float] = []

    result = acceptance._model_readiness_barrier(_environment(), transport=transport, sleeper=quiet.append)

    assert result.dispatch_clear and result.metrics_samples == 3
    assert result.probes_completed == result.usable_responses == 4
    assert quiet == [1.0, 1.0]
    assert transport.maximum_active == 4 and transport.active == 0
    assert transport.paths == _SAMPLE_PATHS * 2 + ["/v1/chat/completions"] * 4 + _SAMPLE_PATHS


@pytest.mark.parametrize(
    ("failure", "posts", "code"),
    [
        ("missing-witness", 0, "metrics_invalid"),
        ("oversized-witness", 0, "metrics_invalid"),
        ("mid-sample-restart", 0, "metrics_invalid"),
        ("before-probe-restart", 0, "metrics_epoch_changed"),
        ("after-probe-restart", 4, "metrics_epoch_changed"),
        ("seed-mismatch", 0, "metrics_invalid"),
        ("wrong-runtime", 0, "metrics_invalid"),
        ("busy-before", 0, "model_busy"),
        ("busy-after", 4, "model_busy"),
        ("metadata-timeout", 0, "metrics_invalid"),
    ],
)
def test_sglang_readiness_never_downgrades_missing_or_changed_identity(
    failure: str, posts: int, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _Transport(failure)
    if failure == "metadata-timeout":
        monkeypatch.setattr(acceptance, "MODEL_READINESS_METRICS_TIMEOUT_SEC", 0.01)

    with pytest.raises(battery.BatteryContractError, match=f"^model_readiness_{code}$"):
        acceptance._model_readiness_barrier(
            _environment(),
            transport=transport,
            sleeper=lambda _seconds: None,
            require_authoritative_metrics=False,
        )

    assert transport.posts == posts and transport.active == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"FRIDAY_LLM_MODEL": "unverified-model"},
        {"FRIDAY_LLM_BASE_URL": "http://127.0.0.1:8001/prefix/v1"},
    ],
)
def test_sglang_readiness_refuses_an_unbound_model_or_endpoint_before_http(changes: dict[str, str]) -> None:
    transport = _Transport()

    with pytest.raises(battery.BatteryContractError, match="^model_readiness_profile_unsupported$"):
        acceptance._model_readiness_barrier(
            {**_environment(), **changes}, transport=transport, sleeper=lambda _seconds: None
        )

    assert not transport.paths


@pytest.mark.asyncio
async def test_sglang_readiness_cancellation_stops_metadata_without_probes() -> None:
    transport = _Transport("metadata-timeout")
    task = asyncio.create_task(
        acceptance._async_model_readiness_barrier(
            _environment(),
            transport=transport,
            sleeper=lambda _seconds: None,
            require_authoritative_metrics=True,
        )
    )
    try:
        await asyncio.wait_for(transport.metadata_entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert task.done() and transport.posts == 0
    assert transport.paths == [_WITNESS]
