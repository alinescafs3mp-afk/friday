"""Model-free regressions for the sealed pre-release live acceptance runner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_acceptance as acceptance  # noqa: E402
import synthetic_live_battery as battery  # noqa: E402


def _private_root(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _isolate_acceptance_lock_protocol(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Fake unit runs must neither contend with real host acceptance nor with a
    # different xdist worker.  Production keeps its fixed protocol constant.
    protocol = f"friday.synthetic-live-acceptance.unit.{os.getpid()}.{tmp_path.name}"
    monkeypatch.setattr(acceptance, "_ACCEPTANCE_LOCK_PROTOCOL", protocol.encode("ascii"))


def test_pre_release_inventory_is_exact_unique_and_candidate_bound() -> None:
    focused = acceptance.inventory_for_suite("focused")
    p06 = acceptance.inventory_for_suite("p06")
    combined = acceptance.inventory_for_suite("all")

    assert focused["pass_ids"] == ["A-P01", "A-P02", "A-P04", "A-P08", "A-P09", "A-P10"]
    assert focused["passes"] == 6
    assert focused["cases"] == 120
    assert p06["pass_ids"] == ["A-P06", "B-P06"]
    assert p06["passes"] == 2
    assert p06["cases"] == 40
    assert combined["passes"] == 8
    assert combined["cases"] == 160
    assert len(combined["pass_ids"]) == len(set(combined["pass_ids"]))
    assert set(focused["pass_ids"]).isdisjoint(p06["pass_ids"])
    assert battery._is_sha256(combined["candidate_source_sha256"])
    assert battery._is_sha256(combined["runner_sha256"])
    candidate_files = battery._candidate_source_paths(instrument_path=acceptance.RUNNER_PATH)
    assert acceptance.RUNNER_RELATIVE_PATH in candidate_files
    assert "tools/synthetic_live_battery.py" in candidate_files
    assert "sol/LIVE_TEST_2026-08-08.md" not in candidate_files
    assert "start.txt" not in candidate_files


def test_all_pass_homes_are_private_and_presealed_before_dispatch(tmp_path: Path) -> None:
    run_root = _private_root(tmp_path / "acceptance")
    sealed = acceptance._preseal_passes("all", run_root, acceptance._load_manifests())

    assert [item.context.pass_id for item in sealed] == [
        "A-P01",
        "A-P02",
        "A-P04",
        "A-P08",
        "A-P09",
        "A-P10",
        "A-P06",
        "B-P06",
    ]
    assert len({case.id for item in sealed for case in item.cases}) == 160
    assert len({case.question for item in sealed for case in item.cases}) == 160
    for item in sealed:
        assert item.context.home.is_dir()
        assert item.context.evidence_path.parent.is_dir()
        assert stat.S_IMODE(item.context.home.stat().st_mode) == 0o700
        assert stat.S_IMODE(item.context.evidence_path.parent.stat().st_mode) == 0o700
    assert acceptance._private_tree(run_root) is True


def test_every_selected_pass_is_dispatched_once_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = _private_root(tmp_path / "acceptance")
    sealed = acceptance._preseal_passes("all", run_root, acceptance._load_manifests())
    candidate_files = (
        "tools/synthetic_live_acceptance.py",
        "tools/synthetic_live_battery.py",
    )
    candidate_digest = "a" * 64
    calls: dict[str, int] = {}
    lock = threading.Lock()

    class FakeExecutor:
        def __init__(self, environment: dict[str, str], *, instrument_path: Path) -> None:
            assert environment == {}
            assert instrument_path == acceptance.RUNNER_PATH
            self._candidate_files = candidate_files
            self._candidate_source_sha256 = candidate_digest

        def __enter__(self) -> FakeExecutor:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def _assert_candidate_unchanged(self) -> None:
            return None

        def __call__(self, _manifest: Any, _pass_spec: Any, cases: Any, context: Any) -> Any:
            # Both homes must exist before either worker is allowed to dispatch.
            assert all(item.context.home.is_dir() for item in sealed)
            with lock:
                calls[context.pass_id] = calls.get(context.pass_id, 0) + 1
            raise RuntimeError("private worker detail must not escape")

    monkeypatch.setattr(battery, "_candidate_source_paths", lambda **_kwargs: candidate_files)
    monkeypatch.setattr(battery, "_candidate_source_digest", lambda **_kwargs: candidate_digest)
    monkeypatch.setattr(battery, "_inherit_model_environment", lambda: {})
    monkeypatch.setattr(battery, "SubprocessPassExecutor", FakeExecutor)

    result = acceptance._execute_sealed(sealed, concurrency=2, model_environment={})

    assert calls == {item.context.pass_id: 1 for item in sealed}
    assert result.dispatches == {item.context.pass_id: 1 for item in sealed}
    assert result.worker_codes == {item.key: "pass_worker_error" for item in sealed}
    assert result.candidate_identity is True


def _readiness_environment() -> dict[str, str]:
    return {
        "FRIDAY_PROFILE": "qwen36-27b-nvfp4-nvidia",
        "FRIDAY_LLM_BASE_URL": "http://127.0.0.1:8001/v1",
        "FRIDAY_EMBEDDINGS_BASE_URL": "http://127.0.0.1:8002/v1",
        "FRIDAY_RERANK_BASE_URL": "http://127.0.0.1:8003/v1",
        "FRIDAY_LLM_MODEL": "dispatcher",
        "FRIDAY_LLM_API_KEY": "synthetic-readiness-key",
    }


_EXPECTED_READINESS_CLASSIFIER_INPUTS = (
    "Найди актуальное расписание TEST-001",
    "Что написано в синтетическом акте TEST-002?",
    "Что писал участник Альфа TEST-003?",
    "Напомни завтра проверить TEST-004",
)
_EXPECTED_READINESS_CLASSIFIER_SYSTEM_SHA256 = (
    "b2a2ae7bf36e8beac9831a2b446443b6d8770998a0e76574ba8d450ce910b1ee"
)
_VALID_READINESS_CLASSIFIER_OUTPUTS = {
    "Найди актуальное расписание TEST-001": {
        "вид": "интернет",
        "запрос": "актуальное расписание TEST-001",
        "кто": "",
        "дни": [],
        "правило": "",
    },
    "Что написано в синтетическом акте TEST-002?": {
        "вид": "архив",
        "запрос": "",
        "кто": "",
        "дни": [],
        "правило": "",
    },
    "Что писал участник Альфа TEST-003?": {
        "вид": "человек",
        "запрос": "",
        "кто": "Альфа TEST-003",
        "дни": [],
        "правило": "",
    },
    "Напомни завтра проверить TEST-004": {
        "вид": "действие",
        "запрос": "",
        "кто": "",
        "дни": [],
        "правило": "",
    },
}


def _readiness_classifier_response(verdict: dict[str, Any]) -> httpx.Response:
    content = json.dumps(
        verdict,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _expected_readiness_classifier_response(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    user_input = payload["messages"][1]["content"]
    return _readiness_classifier_response(_VALID_READINESS_CLASSIFIER_OUTPUTS[user_input])


def _readiness_generation_envelope(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": content}}]}


@pytest.mark.parametrize(
    "environment",
    [
        {
            "FRIDAY_PROFILE": "qwen36-27b-nvfp4-nvidia",
            "FRIDAY_LLM_MODEL": "dispatcher",
        },
        {
            "JERICHO_PROFILE": "qwen36-27b-nvfp4-nvidia",
            "JERICHO_LLM_MODEL": "dispatcher",
        },
        {
            "FRIDAY_PROFILE": " qwen36-27b-nvfp4-nvidia ",
            "JERICHO_PROFILE": "qwen36-27b-nvfp4-nvidia",
            "FRIDAY_LLM_MODEL": "dispatcher ",
            "JERICHO_LLM_MODEL": "dispatcher",
        },
    ],
)
def test_acceptance_requires_the_exact_frozen_dispatcher_environment(
    environment: dict[str, str],
) -> None:
    acceptance._assert_frozen_dispatcher_environment(environment)


@pytest.mark.parametrize(
    ("environment", "code"),
    [
        ({"FRIDAY_LLM_MODEL": "dispatcher"}, "acceptance_profile_missing"),
        (
            {"FRIDAY_PROFILE": "", "FRIDAY_LLM_MODEL": "dispatcher"},
            "acceptance_profile_missing",
        ),
        (
            {"FRIDAY_PROFILE": "qwen36-vl", "FRIDAY_LLM_MODEL": "dispatcher"},
            "acceptance_profile_mismatch",
        ),
        (
            {
                "FRIDAY_PROFILE": "qwen36-27b-nvfp4-nvidia",
                "JERICHO_PROFILE": "qwen36-vl",
                "FRIDAY_LLM_MODEL": "dispatcher",
            },
            "acceptance_profile_alias_conflict",
        ),
        (
            {"FRIDAY_PROFILE": "qwen36-27b-nvfp4-nvidia"},
            "acceptance_model_missing",
        ),
        (
            {
                "FRIDAY_PROFILE": "qwen36-27b-nvfp4-nvidia",
                "FRIDAY_LLM_MODEL": "",
            },
            "acceptance_model_missing",
        ),
        (
            {
                "FRIDAY_PROFILE": "qwen36-27b-nvfp4-nvidia",
                "FRIDAY_LLM_MODEL": "Dispatcher",
            },
            "acceptance_model_mismatch",
        ),
        (
            {
                "FRIDAY_PROFILE": "qwen36-27b-nvfp4-nvidia",
                "FRIDAY_LLM_MODEL": "dispatcher",
                "JERICHO_LLM_MODEL": "another-model",
            },
            "acceptance_model_alias_conflict",
        ),
    ],
)
def test_acceptance_dispatcher_environment_fails_closed(
    environment: dict[str, str],
    code: str,
) -> None:
    with pytest.raises(battery.BatteryContractError, match=rf"^{code}$"):
        acceptance._assert_frozen_dispatcher_environment(environment)


def _vllm_metrics(*, running: int = 0, waiting: int = 0, epoch: int = 1_700_000_000) -> str:
    return (
        f"process_start_time_seconds {epoch}\n"
        f"vllm:num_requests_running {running}\n"
        f"vllm:num_requests_waiting {waiting}\n"
    )


def test_readiness_runs_four_production_shaped_classifier_probes_concurrently() -> None:
    class ClassifierTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.active = 0
            self.maximum_active = 0
            self.release = asyncio.Event()
            self.requests: list[httpx.Request] = []
            self.user_inputs: list[str] = []

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.path == "/metrics":
                return httpx.Response(404)

            assert request.url.path == "/v1/chat/completions"
            payload = json.loads(request.content)
            assert set(payload) == {
                "model",
                "messages",
                "max_tokens",
                "temperature",
                "stream",
                "chat_template_kwargs",
            }
            assert payload["model"] == "dispatcher"
            assert payload["max_tokens"] == 256
            assert payload["temperature"] == 0.0
            assert payload["stream"] is False
            assert payload["chat_template_kwargs"] == {"enable_thinking": False}
            assert "tools" not in payload and "tool_choice" not in payload

            messages = payload["messages"]
            assert [message["role"] for message in messages] == ["system", "user"]
            assert all(set(message) == {"role", "content"} for message in messages)
            system_prompt = messages[0]["content"]
            user_input = messages[1]["content"]
            assert 6_700 <= len(system_prompt) <= 7_000
            assert system_prompt.startswith("Реши, что от тебя хотят, и верни ОДНУ строку JSON:")
            assert system_prompt.endswith("Никаких пояснений, только JSON.")
            assert (
                hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
                == _EXPECTED_READINESS_CLASSIFIER_SYSTEM_SHA256
            )
            assert user_input in _EXPECTED_READINESS_CLASSIFIER_INPUTS
            self.user_inputs.append(user_input)

            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            if self.active == 4:
                self.release.set()
            try:
                await asyncio.wait_for(self.release.wait(), timeout=1.0)
                return _readiness_classifier_response(_VALID_READINESS_CLASSIFIER_OUTPUTS[user_input])
            finally:
                self.active -= 1

    transport = ClassifierTransport()
    result = acceptance._model_readiness_barrier(
        _readiness_environment(),
        transport=transport,
        sleeper=lambda _seconds: None,
        require_authoritative_metrics=False,
    )

    assert result.queue_state == "unknown"
    assert result.metrics_samples == 0
    assert result.probes_requested == result.probes_completed == 4
    assert type(result.maximum_latency_ms) is int and result.maximum_latency_ms >= 0
    assert result.probes_clear is True
    assert result.dispatch_clear is False
    assert transport.maximum_active == 4
    assert transport.active == 0
    assert sorted(transport.user_inputs) == sorted(_EXPECTED_READINESS_CLASSIFIER_INPUTS)
    assert [request.method for request in transport.requests].count("GET") == 1
    assert [request.method for request in transport.requests].count("POST") == 4
    assert all(
        request.headers["authorization"] == "Bearer synthetic-readiness-key" for request in transport.requests
    )


def test_readiness_rejects_one_valid_but_wrong_classifier_kind_for_all_probes() -> None:
    same_wrong_verdict = {
        "вид": "другое",
        "запрос": "",
        "кто": "",
        "дни": [],
        "правило": "",
    }
    seen_inputs: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404)
        payload = json.loads(request.content)
        seen_inputs.append(payload["messages"][1]["content"])
        return _readiness_classifier_response(same_wrong_verdict)

    with pytest.raises(
        battery.BatteryContractError,
        match="^model_readiness_generation_invalid$",
    ):
        acceptance._model_readiness_barrier(
            _readiness_environment(),
            transport=httpx.MockTransport(handler),
            sleeper=lambda _seconds: None,
            require_authoritative_metrics=False,
        )

    assert sorted(seen_inputs) == sorted(_EXPECTED_READINESS_CLASSIFIER_INPUTS)


def test_readiness_rejects_duplicate_classifier_json_keys() -> None:
    duplicate_kind = (
        '{"вид":"интернет","вид":"интернет",'
        '"запрос":"актуальное расписание TEST-001",'
        '"кто":"","дни":[],"правило":""}'
    )
    response = json.dumps(
        {"choices": [{"message": {"content": duplicate_kind}}]},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    assert (
        acceptance._usable_model_readiness_classifier_response(
            response,
            expected_kind="интернет",
        )
        is False
    )


@pytest.mark.parametrize(
    "response_json",
    [
        {"choices": []},
        _readiness_generation_envelope("not JSON"),
        _readiness_generation_envelope(
            json.dumps(
                {"вид": "другое", "запрос": "", "кто": "", "дни": []},
                ensure_ascii=False,
            )
        ),
        _readiness_generation_envelope(
            json.dumps(
                {
                    "вид": "другое",
                    "запрос": "",
                    "кто": "",
                    "дни": [],
                    "правило": "",
                    "лишнее": False,
                },
                ensure_ascii=False,
            )
        ),
        _readiness_generation_envelope(
            json.dumps(
                {"вид": "неизвестно", "запрос": "", "кто": "", "дни": [], "правило": ""},
                ensure_ascii=False,
            )
        ),
        _readiness_generation_envelope(
            json.dumps(
                {"вид": "интернет", "запрос": "", "кто": "", "дни": [], "правило": ""},
                ensure_ascii=False,
            )
        ),
        _readiness_generation_envelope(
            json.dumps(
                {
                    "вид": "архив",
                    "запрос": "",
                    "кто": "",
                    "дни": "завтра",
                    "правило": "",
                },
                ensure_ascii=False,
            )
        ),
    ],
    ids=(
        "empty-choices",
        "malformed-content",
        "missing-field",
        "extra-field",
        "unknown-kind",
        "internet-without-query",
        "wrong-field-type",
    ),
)
def test_readiness_rejects_malformed_or_out_of_schema_http_200(
    response_json: dict[str, Any],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(200, json=response_json)

    with pytest.raises(
        battery.BatteryContractError,
        match="^model_readiness_generation_invalid$",
    ):
        acceptance._model_readiness_barrier(
            _readiness_environment(),
            transport=httpx.MockTransport(handler),
            sleeper=lambda _seconds: None,
            require_authoritative_metrics=False,
        )


def test_readiness_rejects_a_malformed_http_200_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(200, content=b"{not-json")

    with pytest.raises(
        battery.BatteryContractError,
        match="^model_readiness_generation_invalid$",
    ):
        acceptance._model_readiness_barrier(
            _readiness_environment(),
            transport=httpx.MockTransport(handler),
            sleeper=lambda _seconds: None,
            require_authoritative_metrics=False,
        )


@pytest.mark.parametrize("status_code", [201, 204, 503])
def test_readiness_requires_exact_http_200(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(status_code, json={})

    with pytest.raises(
        battery.BatteryContractError,
        match="^model_readiness_generation_failed$",
    ):
        acceptance._model_readiness_barrier(
            _readiness_environment(),
            transport=httpx.MockTransport(handler),
            sleeper=lambda _seconds: None,
            require_authoritative_metrics=False,
        )


def test_official_readiness_refuses_unknown_queue_without_sending_probes() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method != "GET":
            raise AssertionError("official gate must not add work to an unknown queue")
        return httpx.Response(404)

    result = acceptance._model_readiness_barrier(
        _readiness_environment(),
        transport=httpx.MockTransport(handler),
        sleeper=lambda _seconds: None,
    )

    assert result.queue_state == "unknown"
    assert result.probes_requested == 4
    assert result.probes_completed == 0
    assert result.probes_clear is False
    assert result.dispatch_clear is False
    assert methods == ["GET"]


def test_readiness_barrier_refuses_a_busy_vllm_before_generation() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            text=_vllm_metrics(running=1, waiting=2),
        )

    with pytest.raises(battery.BatteryContractError, match="model_readiness_model_busy"):
        acceptance._model_readiness_barrier(
            _readiness_environment(),
            transport=httpx.MockTransport(handler),
            sleeper=lambda _seconds: None,
            require_authoritative_metrics=False,
        )

    assert methods == ["GET"]


def test_readiness_barrier_accepts_only_a_quiet_vllm_probe_cycle() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                text=(
                    "# HELP vllm:num_requests_running Number of requests in model execution.\n"
                    "process_start_time_seconds 1700000000\n"
                    'vllm:num_requests_running{model_name="dispatcher"} 0.0\n'
                    'vllm:num_requests_waiting{model_name="dispatcher"} 0.0\n'
                ),
            )
        return _expected_readiness_classifier_response(request)

    result = acceptance._model_readiness_barrier(
        _readiness_environment(),
        transport=httpx.MockTransport(handler),
        sleeper=lambda _seconds: None,
    )

    assert result.queue_state == "clear"
    assert result.metrics_samples == 3
    assert result.probes_requested == result.probes_completed == 4
    assert result.probes_clear is True
    assert result.dispatch_clear is True
    assert methods[:2] == ["GET", "GET"]
    assert methods[2:6] == ["POST"] * 4
    assert methods[6:] == ["GET"]


@pytest.mark.parametrize(("running", "waiting"), [(1, 0), (0, 1)])
def test_readiness_barrier_requires_vllm_to_stay_idle_after_probe(
    running: int,
    waiting: int,
) -> None:
    metric_samples = iter(
        [
            _vllm_metrics(),
            _vllm_metrics(),
            _vllm_metrics(running=running, waiting=waiting),
        ]
    )
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, text=next(metric_samples))
        return _expected_readiness_classifier_response(request)

    with pytest.raises(battery.BatteryContractError, match="model_readiness_model_busy"):
        acceptance._model_readiness_barrier(
            _readiness_environment(),
            transport=httpx.MockTransport(handler),
            sleeper=lambda _seconds: None,
        )

    assert methods[:2] == ["GET", "GET"]
    assert methods[2:6] == ["POST"] * 4
    assert methods[6:] == ["GET"]


@pytest.mark.parametrize(
    ("epochs", "expected_methods"),
    [
        ([10, 11], ["GET", "GET"]),
        ([10, 10, 11], ["GET", "GET", "POST", "POST", "POST", "POST", "GET"]),
    ],
)
def test_readiness_barrier_rejects_a_metrics_epoch_change(
    epochs: list[int],
    expected_methods: list[str],
) -> None:
    metric_samples = iter([_vllm_metrics(epoch=epoch) for epoch in epochs])
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, text=next(metric_samples))
        return _expected_readiness_classifier_response(request)

    with pytest.raises(battery.BatteryContractError, match="model_readiness_metrics_epoch_changed"):
        acceptance._model_readiness_barrier(
            _readiness_environment(),
            transport=httpx.MockTransport(handler),
            sleeper=lambda _seconds: None,
        )

    assert methods == expected_methods


def test_readiness_barrier_really_starts_four_probes_concurrently() -> None:
    class ConcurrentTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.active = 0
            self.maximum_active = 0
            self.release = asyncio.Event()

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(404)
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            if self.active == 4:
                self.release.set()
            try:
                await asyncio.wait_for(self.release.wait(), timeout=1)
                return _expected_readiness_classifier_response(request)
            finally:
                self.active -= 1

    transport = ConcurrentTransport()
    result = acceptance._model_readiness_barrier(
        _readiness_environment(),
        transport=transport,
        sleeper=lambda _seconds: None,
        require_authoritative_metrics=False,
    )

    assert result.queue_state == "unknown"
    assert result.probes_clear is True
    assert result.dispatch_clear is False
    assert transport.maximum_active == 4
    assert transport.active == 0


def test_readiness_deadline_cancels_and_drains_hanging_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HangingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.active = 0
            self.cancelled = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(404)
            self.active += 1
            try:
                await asyncio.Event().wait()
                raise AssertionError("unreachable")
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            finally:
                self.active -= 1

    monkeypatch.setattr(acceptance, "MODEL_READINESS_BUDGET_SEC", 0.05)
    monkeypatch.setattr(acceptance, "MODEL_READINESS_GENERATION_TIMEOUT_SEC", 0.05)
    transport = HangingTransport()
    started = time.monotonic()

    with pytest.raises(battery.BatteryContractError, match="model_readiness_deadline_exhausted"):
        acceptance._model_readiness_barrier(
            _readiness_environment(),
            transport=transport,
            sleeper=lambda _seconds: None,
            require_authoritative_metrics=False,
        )

    assert time.monotonic() - started < 1.0
    assert transport.cancelled == 4
    assert transport.active == 0


def test_readiness_barrier_sanitizes_remote_failures() -> None:
    secret = "remote-body-with-private-token"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404)
        raise RuntimeError(secret)

    with pytest.raises(battery.BatteryContractError) as failure:
        acceptance._model_readiness_barrier(
            _readiness_environment(),
            transport=httpx.MockTransport(handler),
            sleeper=lambda _seconds: None,
            require_authoritative_metrics=False,
        )

    assert str(failure.value) == "model_readiness_probe_failed"
    assert secret not in str(failure.value)


def test_readiness_failure_prevents_acceptance_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_acceptance_lock_protocol(monkeypatch, tmp_path)
    run_root = tmp_path / "acceptance"
    dispatched = False

    def unknown(_environment: Any, **_kwargs: Any) -> acceptance.ModelReadinessResult:
        return acceptance.ModelReadinessResult(
            queue_state="unknown",
            metrics_samples=0,
            probes_requested=4,
            probes_completed=0,
            maximum_latency_ms=0,
        )

    def refuse_dispatch(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal dispatched
        dispatched = True
        raise AssertionError("readiness failure must precede dispatch")

    monkeypatch.setattr(battery, "_assert_ignored_or_external", lambda _path: None)
    monkeypatch.setattr(
        acceptance,
        "_acceptance_lock_path",
        lambda: tmp_path / "runtime" / "locks" / "acceptance.lock",
    )
    monkeypatch.setattr(battery, "_inherit_model_environment", _readiness_environment)
    monkeypatch.setattr(acceptance, "_model_readiness_barrier", unknown)
    monkeypatch.setattr(acceptance, "_execute_sealed", refuse_dispatch)

    with pytest.raises(battery.BatteryContractError, match="model_readiness_result_invalid"):
        acceptance.run_acceptance(
            "p06",
            run_directory=run_root,
            concurrency=4,
            artifact_id="PRE-RELEASE-P06-0123456789abcdef",
        )

    assert dispatched is False


def test_readiness_timeout_is_closed_before_any_acceptance_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_acceptance_lock_protocol(monkeypatch, tmp_path)
    run_root = tmp_path / "acceptance"
    dispatched = False

    def timed_out(*_args: Any, **_kwargs: Any) -> acceptance.ModelReadinessResult:
        raise battery.BatteryContractError("model_readiness_deadline_exhausted")

    def refuse_dispatch(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal dispatched
        dispatched = True
        raise AssertionError("a readiness timeout must precede worker dispatch")

    monkeypatch.setattr(battery, "_assert_ignored_or_external", lambda _path: None)
    monkeypatch.setattr(
        acceptance,
        "_acceptance_lock_path",
        lambda: tmp_path / "runtime" / "locks" / "acceptance.lock",
    )
    monkeypatch.setattr(battery, "_inherit_model_environment", _readiness_environment)
    monkeypatch.setattr(acceptance, "_model_readiness_barrier", timed_out)
    monkeypatch.setattr(acceptance, "_execute_sealed", refuse_dispatch)

    with pytest.raises(
        battery.BatteryContractError,
        match="^model_readiness_deadline_exhausted$",
    ):
        acceptance.run_acceptance(
            "all",
            run_directory=run_root,
            concurrency=4,
            artifact_id="PRE-RELEASE-ALL-0123456789abcdef",
        )

    assert dispatched is False


def test_invalid_dispatcher_profile_precedes_readiness_and_every_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_acceptance_lock_protocol(monkeypatch, tmp_path)
    run_root = tmp_path / "must-not-be-created"
    readiness_called = False
    dispatch_called = False

    def refuse_readiness(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal readiness_called
        readiness_called = True
        raise AssertionError("profile mismatch must precede readiness")

    def refuse_dispatch(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal dispatch_called
        dispatch_called = True
        raise AssertionError("profile mismatch must precede dispatch")

    environment = _readiness_environment()
    environment["FRIDAY_PROFILE"] = "qwen36-vl"
    monkeypatch.setattr(
        acceptance,
        "_acceptance_lock_path",
        lambda: tmp_path / "runtime" / "locks" / "acceptance.lock",
    )
    monkeypatch.setattr(battery, "_inherit_model_environment", lambda: environment)
    monkeypatch.setattr(acceptance, "_model_readiness_barrier", refuse_readiness)
    monkeypatch.setattr(acceptance, "_execute_sealed", refuse_dispatch)

    with pytest.raises(battery.BatteryContractError, match="^acceptance_profile_mismatch$"):
        acceptance.run_acceptance(
            "all",
            run_directory=run_root,
            concurrency=4,
            artifact_id="PRE-RELEASE-ALL-0123456789abcdef",
        )

    assert readiness_called is False
    assert dispatch_called is False
    assert run_root.exists() is False


def test_official_all_acceptance_refuses_reduced_execution_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_acceptance_lock_protocol(monkeypatch, tmp_path)
    with pytest.raises(
        battery.BatteryContractError,
        match="acceptance_execution_concurrency_invalid",
    ):
        acceptance.run_acceptance(
            "all",
            run_directory=tmp_path / "must-not-be-created",
            concurrency=3,
            artifact_id="PRE-RELEASE-ALL-0123456789abcdef",
        )

    assert not (tmp_path / "must-not-be-created").exists()


def test_sanitized_summary_binds_probe_and_execution_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_acceptance_lock_protocol(monkeypatch, tmp_path)
    run_root = tmp_path / "acceptance"
    runtime_digest = "a" * 64
    candidate_digest = "b" * 64

    def fake_execution(
        sealed: Any,
        *,
        concurrency: int,
        model_environment: dict[str, str],
    ) -> acceptance.ExecutionResult:
        assert concurrency == 4
        assert model_environment == _readiness_environment()
        return acceptance.ExecutionResult(
            results={},
            worker_codes={item.key: "" for item in sealed},
            dispatches={item.context.pass_id: 1 for item in sealed},
            candidate_files=(acceptance.RUNNER_RELATIVE_PATH,),
            candidate_pre_sha256=candidate_digest,
            candidate_sealed_sha256=candidate_digest,
            candidate_post_sha256=candidate_digest,
        )

    def fake_pass_row(item: Any, _execution: Any) -> dict[str, Any]:
        return {
            "pass_id": item.context.pass_id,
            "cases": 20,
            "passed": 20,
            "failed": 0,
            "all_gates_exact": True,
            "runtime_sha256": runtime_digest,
        }

    monkeypatch.setattr(battery, "_assert_ignored_or_external", lambda _path: None)
    monkeypatch.setattr(
        acceptance,
        "_acceptance_lock_path",
        lambda: tmp_path / "runtime" / "locks" / "acceptance.lock",
    )
    monkeypatch.setattr(battery, "_inherit_model_environment", _readiness_environment)
    monkeypatch.setattr(
        acceptance,
        "_model_readiness_barrier",
        lambda _environment, **_kwargs: acceptance.ModelReadinessResult(
            queue_state="unknown",
            metrics_samples=0,
            probes_requested=4,
            probes_completed=4,
            maximum_latency_ms=123,
        ),
    )
    monkeypatch.setattr(acceptance, "_execute_sealed", fake_execution)
    monkeypatch.setattr(acceptance, "_summarize_pass", fake_pass_row)
    monkeypatch.setattr(
        acceptance,
        "_p06_summary",
        lambda *_args, **_kwargs: {"status": "green"},
    )

    code, summary = acceptance.run_acceptance(
        "p06",
        run_directory=run_root,
        concurrency=4,
        artifact_id="PRE-RELEASE-P06-0123456789abcdef",
    )

    assert code == 0
    assert summary["status"] == "green"
    assert summary["model_readiness_queue_state"] == "unknown"
    assert summary["model_readiness_dispatch_clear"] is False
    assert summary["model_readiness_concurrency"] == 4
    assert summary["model_readiness_probes_completed"] == 4
    assert summary["model_readiness_probes_clear"] is True
    assert summary["execution_concurrency"] == 4
    assert summary["execution_concurrency_exact"] is True


def _closed_reconciliation(kind: str) -> dict[str, Any]:
    if kind == "pass":
        value: dict[str, Any] = {
            "schema": battery.RECONCILIATION_SCHEMA,
            "clear": True,
            "api_exact": True,
            "audit_exact": True,
            "counters_exact": True,
            "files_exact": True,
            "http_exact": True,
            "storage_exact": True,
            "tools_exact": True,
        }
    else:
        value = {
            "schema": "friday.synthetic-live-battery.tail-reconciliation.v1",
            "clear": True,
            "probe_exact": True,
            "files_exact": True,
            "database_exact": True,
        }
    value["snapshot_sha256"] = battery._sha256_bytes(battery._canonical_json_bytes(value))
    return value


def test_missing_tail_and_wrong_combined_digest_fail_closed(tmp_path: Path) -> None:
    run_root = _private_root(tmp_path / "acceptance")
    item = acceptance._preseal_passes("p06", run_root, acceptance._load_manifests())[0]
    evidence_path = item.context.evidence_path
    battery._secure_write_bytes(evidence_path, b"closed synthetic evidence\n")
    pass_reconciliation = _closed_reconciliation("pass")
    tail_reconciliation = _closed_reconciliation("tail")
    battery._secure_write_json(
        evidence_path.parent / "pass-reconciliation.json",
        pass_reconciliation,
    )
    tail_path = evidence_path.parent / "tail-reconciliation.json"
    battery._secure_write_json(tail_path, tail_reconciliation)
    pass_full_hash = battery._sha256_bytes(battery._canonical_json_bytes(pass_reconciliation))
    combined_hash = battery._sha256_bytes(
        battery._canonical_json_bytes(
            {
                "pass_reconciliation_sha256": pass_full_hash,
                "tail_reconciliation_sha256": tail_reconciliation["snapshot_sha256"],
            }
        )
    )
    result = {
        "pass_id": item.context.pass_id,
        "block": str(item.pass_spec["block"]),
        "cases": 20,
        "passed": 20,
        "failed": 0,
        "case_results": [
            {
                "case_id": case.id,
                "passed": True,
                "failure_codes": [],
                "response_sha256": "b" * 64,
                "latency_ms": 1,
                "privacy_canary_clear": True,
            }
            for case in item.cases
        ],
        "evidence_sha256": battery.file_sha256(evidence_path),
        "runtime_hash": "c" * 64,
        "pass_reconciliation_clear": True,
        "pass_reconciliation_sha256": combined_hash,
    }
    execution = acceptance.ExecutionResult(
        results={item.key: result},
        worker_codes={item.key: ""},
        dispatches={item.context.pass_id: 1},
        candidate_files=(acceptance.RUNNER_RELATIVE_PATH,),
        candidate_pre_sha256="d" * 64,
        candidate_sealed_sha256="d" * 64,
        candidate_post_sha256="d" * 64,
    )
    assert acceptance._summarize_pass(item, execution)["all_gates_exact"] is True

    forged = dict(result)
    forged["pass_reconciliation_sha256"] = "e" * 64
    forged_execution = replace(execution, results={item.key: forged})
    assert acceptance._summarize_pass(item, forged_execution)["all_gates_exact"] is False

    tail_path.rename(evidence_path.parent / "tail-reconciliation.missing")
    assert acceptance._summarize_pass(item, execution)["all_gates_exact"] is False


def test_reconciliation_and_private_tree_fail_closed(tmp_path: Path) -> None:
    root = _private_root(tmp_path / "evidence")
    components = {
        "api_exact": True,
        "audit_exact": True,
        "counters_exact": True,
        "files_exact": True,
        "http_exact": True,
        "storage_exact": True,
        "tools_exact": True,
    }
    value: dict[str, Any] = {
        "schema": battery.RECONCILIATION_SCHEMA,
        "clear": True,
        **components,
    }
    value["snapshot_sha256"] = battery._sha256_bytes(battery._canonical_json_bytes(value))
    path = root / "pass-reconciliation.json"
    battery._secure_write_json(path, value)

    clear, snapshot, observed, full_hash = acceptance._read_reconciliation(path, kind="pass")
    assert clear is True
    assert snapshot == value["snapshot_sha256"]
    assert observed == components
    assert battery._is_sha256(full_hash)
    assert acceptance._private_tree(root) is True

    path.chmod(0o644)
    assert acceptance._read_reconciliation(path, kind="pass")[0] is False
    assert acceptance._private_tree(root) is False

    path.chmod(0o600)
    fifo = root / "unexpected-fifo"
    os.mkfifo(fifo, mode=0o600)
    assert acceptance._private_tree(root) is False


def test_cli_has_no_retry_resume_resubmit_or_repair_path() -> None:
    parser = acceptance._parser()
    option_strings = {option for action in parser._actions for option in action.option_strings}

    assert not option_strings.intersection(
        {
            "--retry",
            "--retries",
            "--resume",
            "--resubmit",
            "--repair",
            "--rerun-failed",
        }
    )
    assert "--env-file" in option_strings


def test_acceptance_audit_only_does_not_select_or_read_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_path = tmp_path / "do-not-read-this-config.env"

    def refuse(_path: Path) -> None:
        raise AssertionError("audit-only must not select an environment file")

    def refuse_model_contract(_environment: Any) -> None:
        raise AssertionError("audit-only must not inspect the live model contract")

    monkeypatch.setattr(battery, "_select_live_env_file", refuse)
    monkeypatch.setattr(acceptance, "_assert_frozen_dispatcher_environment", refuse_model_contract)

    assert acceptance.main(["--suite", "all", "--audit-only", "--env-file", str(private_path)]) == 0
    output = capsys.readouterr().out
    assert private_path.name not in output
    assert json.loads(output)["valid"] is True


def test_acceptance_selects_explicit_env_without_publishing_its_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_path = tmp_path / "operator-private-config-name.env"
    selected: Path | None = None

    def fake_select(path: Path) -> None:
        nonlocal selected
        selected = path

    def fake_run(
        suite: str,
        *,
        run_directory: Path,
        concurrency: int,
        artifact_id: str,
    ) -> tuple[int, dict[str, Any]]:
        assert suite == "all"
        assert run_directory.parent == ROOT / "data" / "live-battery-runs"
        assert concurrency == battery.DEFAULT_CONCURRENCY
        return 4, {
            "schema": acceptance.SUMMARY_SCHEMA,
            "artifact_id": artifact_id,
            "status": "red",
        }

    monkeypatch.setattr(battery, "_select_live_env_file", fake_select)
    monkeypatch.setattr(acceptance, "run_acceptance", fake_run)

    assert acceptance.main(["--env-file", str(private_path)]) == 4
    output = capsys.readouterr().out
    assert selected == private_path
    assert private_path.name not in output
    assert str(private_path) not in output
    assert "env_file" not in json.loads(output)


def test_acceptance_env_preflight_failure_is_sanitized(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_path = tmp_path / "private-config-basename.env"

    assert acceptance.main(["--env-file", str(private_path)]) == 4

    streams = capsys.readouterr()
    public = json.loads(streams.out)
    assert public["status"] == "red"
    assert public["code"] == "pre_release_runner_failed"
    assert private_path.name not in streams.out
    assert private_path.name not in streams.err


@pytest.mark.parametrize(
    "key",
    ["FRIDAY_LLM_BASE_URL", "FRIDAY_EMBEDDINGS_BASE_URL", "FRIDAY_RERANK_BASE_URL"],
)
def test_release_endpoints_require_numeric_local_addresses(key: str) -> None:
    safe = {
        "FRIDAY_LLM_BASE_URL": "http://127.0.0.1:8001/v1",
        "FRIDAY_EMBEDDINGS_BASE_URL": "http://127.0.0.1:8002/v1",
        "FRIDAY_RERANK_BASE_URL": "http://192.168.1.20:8003/v1",
    }
    assert set(battery._configured_model_endpoint_urls(safe)) == {"model", "embedding", "reranker"}

    hostname = dict(safe)
    hostname[key] = "http://localhost:8001/v1"
    with pytest.raises(battery.BatteryContractError, match="worker_relay_endpoint_invalid"):
        battery._configured_model_endpoint_urls(hostname)

    public = dict(safe)
    public[key] = "http://8.8.8.8:8001/v1"
    with pytest.raises(battery.BatteryContractError, match="worker_relay_endpoint_invalid"):
        battery._configured_model_endpoint_urls(public)


def test_custom_run_directory_name_never_reaches_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_name = "customer-secret-project-name"
    run_directory = tmp_path / secret_name
    captured_artifact_id = ""

    def fake_run(
        suite: str,
        *,
        run_directory: Path,
        concurrency: int,
        artifact_id: str,
    ) -> tuple[int, dict[str, Any]]:
        nonlocal captured_artifact_id
        assert suite == "p06"
        assert run_directory.name == secret_name
        assert concurrency == 1
        captured_artifact_id = artifact_id
        return 4, {
            "schema": acceptance.SUMMARY_SCHEMA,
            "artifact_id": artifact_id,
            "status": "red",
        }

    monkeypatch.setattr(acceptance, "run_acceptance", fake_run)

    assert (
        acceptance.main(
            [
                "--suite",
                "p06",
                "--concurrency",
                "1",
                "--run-directory",
                str(run_directory),
            ]
        )
        == 4
    )
    output = capsys.readouterr().out
    assert secret_name not in output
    assert re.fullmatch(r"PRE-RELEASE-P06-[0-9a-f]{16}", captured_artifact_id)
    assert json.loads(output)["artifact_id"] == captured_artifact_id


def test_default_artifact_id_is_the_default_directory_locator(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured_directory: Path | None = None

    def fake_run(
        suite: str,
        *,
        run_directory: Path,
        concurrency: int,
        artifact_id: str,
    ) -> tuple[int, dict[str, Any]]:
        nonlocal captured_directory
        assert suite == "all"
        assert concurrency == battery.DEFAULT_CONCURRENCY
        captured_directory = run_directory
        return 4, {
            "schema": acceptance.SUMMARY_SCHEMA,
            "artifact_id": artifact_id,
            "status": "red",
        }

    monkeypatch.setattr(acceptance, "run_acceptance", fake_run)

    assert acceptance.main([]) == 4
    public = json.loads(capsys.readouterr().out)
    assert captured_directory is not None
    assert captured_directory.parent == ROOT / "data" / "live-battery-runs"
    assert captured_directory.name == public["artifact_id"]


def test_existing_run_directory_is_refused_without_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_directory = _private_root(tmp_path / "existing-private-directory")
    before = tuple(run_directory.iterdir())

    assert acceptance.main(["--suite", "p06", "--run-directory", str(run_directory)]) == 4

    assert tuple(run_directory.iterdir()) == before
    public = json.loads(capsys.readouterr().out)
    assert public["status"] == "red"
    assert public["code"] == "pre_release_runner_failed"
    assert run_directory.name not in json.dumps(public)
