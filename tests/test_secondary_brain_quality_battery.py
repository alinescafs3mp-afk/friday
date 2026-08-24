"""Credential- and content-safe contracts for the secondary live quality battery."""

from __future__ import annotations

import importlib
import json
import re
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "deploy" / "secondary-brain" / "windows-sglang" / "scripts"
API_KEY = "a" * 64
CA_FILE = Path("private-ca-for-mocked-tests.crt")


@pytest.fixture(scope="module", autouse=True)
def _script_import_path() -> Iterator[None]:
    sys.path.insert(0, str(SCRIPTS))
    try:
        yield
    finally:
        sys.path.remove(str(SCRIPTS))


@pytest.fixture(scope="module")
def battery() -> Any:
    return importlib.import_module("quality_battery")


_LIVE_OUTPUTS = {
    "ordinary_ru": "Узел работает.",
    "ordinary_en": "Node ready.",
    "strict_json_ru": '{"язык":"русский","число":17}',
    "strict_json_en": '{"language":"English","number":17}',
    "reasoning_low": "42",
    "reasoning_medium": "42",
    "reasoning_high": "42",
    "no_tool": "No tool needed.",
    "multi_turn": "СЕВЕР-17",
    "long_system": "LONG-SYSTEM-OK",
    "unicode_file_numbers": "Проекты/Ёж №17 — финал.txt | 12345",
    "stop_sequence": "alpha",
    "max_token_truncation": "1 2 3 4 5 6 7 8",
    "arithmetic": "42",
    "extraction_and_date": '{"amount":17,"date":"2026-08-24","person":"Артемьев"}',
    "ru_summary_faithfulness": "Проект «Север»: бюджет 17 рублей, срок 24.08.2026.",
    "contradiction": "CONTRADICTION",
    "citation_preservation": "The measured height is 42 m [SRC-17].",
    "wrong_language_guard": "подтверждено",
}


class FakeEndpoint:
    def __init__(
        self,
        battery: Any,
        *,
        overrides: Mapping[str, str] | None = None,
        invalid_tool: bool = False,
        include_tool_index: bool = True,
        tool_index: object = 0,
        alias_ok: bool = True,
    ) -> None:
        self.battery = battery
        self.overrides = dict(overrides or {})
        self.invalid_tool = invalid_tool
        self.include_tool_index = include_tool_index
        self.tool_index = tool_index
        self.alias_ok = alias_ok
        self.live_names = [case.name for case in battery._live_cases()]
        self.live_index = 0
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        method: str,
        url: str,
        *,
        api_key: str,
        payload: dict[str, Any] | None,
        timeout_sec: float,
        ca_file: Path | None,
    ) -> tuple[dict[str, Any], float]:
        assert api_key == API_KEY
        assert url.startswith("https://192.168.1.35:8443/v1/")
        assert timeout_sec == 10.0
        assert ca_file == CA_FILE
        copied_payload = json.loads(json.dumps(payload, ensure_ascii=False)) if payload is not None else None
        self.calls.append({"method": method, "url": url, "payload": copied_payload})
        if method == "GET":
            model = self.battery.EXPECTED_MODEL if self.alias_ok else "wrong-model"
            return {"data": [{"id": model}]}, 0.01
        assert payload is not None
        assert payload["model"] == self.battery.EXPECTED_MODEL
        assert payload["reasoning_effort"] in {"low", "medium", "high"}
        assert payload["temperature"] == 1.0
        assert payload["top_p"] == 1.0
        assert payload["seed"] == 0
        messages = payload.get("messages")
        assert isinstance(messages, list)
        if any(isinstance(message, dict) and message.get("role") == "tool" for message in messages):
            assert payload["reasoning_effort"] == "low"
            assert payload["max_tokens"] == 256
            return self._completion("17", reasoning="PRIVATE_CONTINUATION_REASONING")
        if isinstance(payload.get("tool_choice"), dict):
            assert payload["reasoning_effort"] == "low"
            assert payload["max_tokens"] == 256
            city = "Paris" if self.invalid_tool else "Moscow"
            tool_call = {
                "id": "call-safe-17",
                "type": "function",
                "function": {
                    "name": "lookup_temperature",
                    "arguments": json.dumps({"city": city}),
                },
            }
            if self.include_tool_index:
                tool_call["index"] = self.tool_index
            return (
                {
                    "model": self.battery.EXPECTED_MODEL,
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "reasoning_content": "PRIVATE_TOOL_REASONING",
                                "tool_calls": [tool_call],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 21, "completion_tokens": 5},
                },
                0.02,
            )
        name = self.live_names[self.live_index]
        self.live_index += 1
        assert payload["reasoning_effort"] == {
            "reasoning_medium": "medium",
            "reasoning_high": "high",
        }.get(name, "low")
        assert payload["max_tokens"] == {
            "reasoning_medium": 512,
            "reasoning_high": 1024,
            "max_token_truncation": 8,
            "stop_sequence": 128,
        }.get(name, 256)
        content = self.overrides.get(name, _LIVE_OUTPUTS[name])
        finish_reason = "length" if name == "max_token_truncation" else "stop"
        completion_tokens = 8 if name == "max_token_truncation" else 5
        reasoning = "PRIVATE_REASONING" if name.startswith("reasoning_") else None
        return self._completion(
            content,
            finish_reason=finish_reason,
            completion_tokens=completion_tokens,
            reasoning=reasoning,
        )

    def _completion(
        self,
        content: str,
        *,
        finish_reason: str = "stop",
        completion_tokens: int = 5,
        reasoning: str | None = None,
    ) -> tuple[dict[str, Any], float]:
        return (
            {
                "model": self.battery.EXPECTED_MODEL,
                "choices": [
                    {
                        "message": {"content": content, "reasoning_content": reasoning},
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": completion_tokens},
            },
            0.03,
        )


def _run_with_fake(monkeypatch: pytest.MonkeyPatch, battery: Any, fake: FakeEndpoint) -> dict[str, Any]:
    monkeypatch.setattr(battery, "request_json", fake)
    monkeypatch.setattr(
        battery,
        "evidence_identity",
        lambda: {
            "candidate_profile_id": "test-profile",
            "candidate_profile_sha256": "c" * 64,
            "served_model_alias": battery.EXPECTED_MODEL,
            "gateway_ca_certificate_sha256": "b" * 64,
        },
    )
    monkeypatch.setattr(
        battery,
        "_run_disconnect_protocol",
        lambda **_kwargs: [
            battery._evidence("stream_cancellation", passed=True),
            battery._evidence("client_disconnect_recovery", passed=True),
        ],
    )
    return battery.run_battery(
        base_url="https://192.168.1.35:8443/v1",
        api_key=API_KEY,
        timeout_sec=10.0,
        ca_file=CA_FILE,
    )


def test_full_battery_covers_the_brief_and_retains_only_closed_evidence(
    monkeypatch: pytest.MonkeyPatch, battery: Any
) -> None:
    fake = FakeEndpoint(battery)
    report = _run_with_fake(monkeypatch, battery, fake)

    required = {
        "exact_model_alias",
        "ordinary_ru",
        "ordinary_en",
        "strict_json_ru",
        "strict_json_en",
        "reasoning_low",
        "reasoning_medium",
        "reasoning_high",
        "no_tool",
        "tool_call_shape",
        "tool_result_continuation",
        "stream_cancellation",
        "client_disconnect_recovery",
        "multi_turn",
        "long_system",
        "unicode_file_numbers",
        "stop_sequence",
        "max_token_truncation",
        "arithmetic",
        "extraction_and_date",
        "ru_summary_faithfulness",
        "contradiction",
        "citation_preservation",
        "wrong_language_guard",
        "reject_empty",
        "reject_nan",
        "reject_degeneration",
        "reject_harmony",
    }
    assert set(report) == {
        "schema",
        "status",
        "candidate_profile_id",
        "candidate_profile_sha256",
        "served_model_alias",
        "gateway_ca_certificate_sha256",
        "cases",
        "raw_content_retained",
        "api_key_retained",
    }
    assert report["schema"] == "friday.secondary-quality-battery.v1"
    assert report["status"] == "passed"
    assert report["raw_content_retained"] is False
    assert report["api_key_retained"] is False
    rows = report["cases"]
    assert isinstance(rows, list)
    assert {row["case"] for row in rows} == required
    assert len(rows) == len(required)
    assert all(set(row) == battery._EVIDENCE_KEYS for row in rows)
    assert all(row["status"] == "passed" for row in rows)
    assert all(
        row["output_sha256"] == "" or re.fullmatch(r"[0-9a-f]{64}", row["output_sha256"]) for row in rows
    )

    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    forbidden = {
        API_KEY,
        "PRIVATE_REASONING",
        "PRIVATE_TOOL_REASONING",
        "PRIVATE_CONTINUATION_REASONING",
        "Узел работает.",
        "No tool needed.",
        "Проекты/Ёж №17 — финал.txt | 12345",
        "Return final content only",
    }
    assert not any(value in serialized for value in forbidden)

    continuation = next(
        call["payload"]
        for call in fake.calls
        if isinstance(call["payload"], dict)
        and any(message.get("role") == "tool" for message in call["payload"]["messages"])
    )
    continuation_text = json.dumps(continuation, ensure_ascii=False)
    assert "PRIVATE_TOOL_REASONING" not in continuation_text
    assistant = next(message for message in continuation["messages"] if message["role"] == "assistant")
    tool_result = next(message for message in continuation["messages"] if message["role"] == "tool")
    assert set(assistant) == {"role", "content", "tool_calls"}
    assert assistant["content"] is None
    assert "index" not in assistant["tool_calls"][0]
    assert tool_result == {
        "role": "tool",
        "tool_call_id": "call-safe-17",
        "content": '{"temperature_c":17}',
    }


def test_deterministic_validator_failure_is_hashed_but_not_retained(
    monkeypatch: pytest.MonkeyPatch, battery: Any
) -> None:
    fake = FakeEndpoint(battery, overrides={"wrong_language_guard": "confirmed"})
    report = _run_with_fake(monkeypatch, battery, fake)

    assert report["status"] == "failed"
    row = next(item for item in report["cases"] if item["case"] == "wrong_language_guard")
    assert row["status"] == "failed"
    assert re.fullmatch(r"[0-9a-f]{64}", row["output_sha256"])
    assert "confirmed" not in json.dumps(report, ensure_ascii=False)


def test_intentional_reasoning_only_length_stop_is_valid_and_content_free(
    monkeypatch: pytest.MonkeyPatch, battery: Any
) -> None:
    fake = FakeEndpoint(battery, overrides={"max_token_truncation": ""})
    report = _run_with_fake(monkeypatch, battery, fake)

    row = next(item for item in report["cases"] if item["case"] == "max_token_truncation")
    assert report["status"] == "passed"
    assert row["status"] == "passed"
    assert re.fullmatch(r"[0-9a-f]{64}", row["output_sha256"])
    assert "PRIVATE_REASONING" not in json.dumps(report, ensure_ascii=False)


@pytest.mark.parametrize("content", ["42", "42.0", "42.000"])
def test_reasoning_validator_accepts_equivalent_integer_renderings(battery: Any, content: str) -> None:
    completion = battery.SanitizedCompletion(
        content=content,
        latency_sec=0.1,
        prompt_tokens=20,
        completion_tokens=20,
        finish_reason="stop",
        reasoning_present=True,
    )

    assert battery._integer_with_separated_reasoning(42)(completion)


@pytest.mark.parametrize(
    ("content", "finish_reason", "reasoning_present"),
    [
        ("42.5", "stop", True),
        ("The answer is 42", "stop", True),
        ("42", "length", True),
        ("42", "stop", False),
    ],
)
def test_reasoning_validator_rejects_wrong_or_unseparated_results(
    battery: Any, content: str, finish_reason: str, reasoning_present: bool
) -> None:
    completion = battery.SanitizedCompletion(
        content=content,
        latency_sec=0.1,
        prompt_tokens=20,
        completion_tokens=20,
        finish_reason=finish_reason,
        reasoning_present=reasoning_present,
    )

    assert not battery._integer_with_separated_reasoning(42)(completion)


@pytest.mark.parametrize(
    "content",
    [
        "Проект «Север»: бюджет 17 рублей, срок 24.08.2026.",
        "Срок проекта «Север» — 24 августа 2026 года, а бюджет составляет 17 руб.",
        "Проект «Север» с бюджетом 17 рублей должен быть завершён 24.08.2026.",
        "Проект «Север» с бюджетом 17 рублей будет завершён до 24.08.2026.",
        "Проект «Север» планируется сдать 24 августа 2026 года при бюджете 17 руб.",
    ],
)
def test_summary_validator_accepts_closed_faithful_deadline_forms(battery: Any, content: str) -> None:
    completion = battery.SanitizedCompletion(
        content=content,
        latency_sec=0.1,
        prompt_tokens=20,
        completion_tokens=20,
        finish_reason="stop",
        reasoning_present=False,
    )

    assert battery._summary_is_faithful(completion)


@pytest.mark.parametrize(
    ("content", "finish_reason"),
    [
        ("Проект «Север»: стоимость 17 рублей, срок 24.08.2026.", "stop"),
        ("Проект «Север»: бюджет 17 долларов, срок 24.08.2026.", "stop"),
        ("Проект «Север»: бюджет 17 рублей, дата 24.08.2026.", "stop"),
        ("Проект «Север» с бюджетом 17 рублей будет обсуждён до 24.08.2026.", "stop"),
        ("Проект «Север»: бюджет 18 рублей, срок 24.08.2026.", "stop"),
        ("Проект «Север»: бюджет 17 рублей, срок 25.08.2026.", "stop"),
        ("Проект «Север»: бюджет 17 рублей, срок 24.08.2026, резерв 9 рублей.", "stop"),
        (
            "Проект «Север»: бюджет 17 рублей, срок 24.08.2026 (24 августа 2026 года).",
            "stop",
        ),
        ("Проект «Север»: бюджет 17 рублей, срок 24.08.2026.", "length"),
    ],
)
def test_summary_validator_rejects_changed_missing_or_extra_facts(
    battery: Any, content: str, finish_reason: str
) -> None:
    completion = battery.SanitizedCompletion(
        content=content,
        latency_sec=0.1,
        prompt_tokens=20,
        completion_tokens=20,
        finish_reason=finish_reason,
        reasoning_present=False,
    )

    assert not battery._summary_is_faithful(completion)


def test_summary_prompt_declares_the_closed_fact_contract(battery: Any) -> None:
    case = next(case for case in battery._live_cases() if case.name == "ru_summary_faithfulness")
    prompt = json.dumps(case.messages, ensure_ascii=False).casefold()

    assert all(item in prompt for item in ("проект", "север", "бюджет", "17 рублей", "срок"))
    assert "без новых фактов или чисел" in prompt
    assert "24.08.2026" in prompt
    assert "24 августа 2026 года" in prompt


@pytest.mark.parametrize(
    "content",
    [
        "The measured height is 42 m [SRC-17].",
        "The recorded height measured 42 meters [SRC-17].",
    ],
)
def test_citation_validator_accepts_faithful_rewrites(battery: Any, content: str) -> None:
    completion = battery.SanitizedCompletion(
        content=content,
        latency_sec=0.1,
        prompt_tokens=20,
        completion_tokens=20,
        finish_reason="stop",
        reasoning_present=False,
    )

    assert battery._citation_is_preserved(completion)


@pytest.mark.parametrize(
    ("content", "finish_reason"),
    [
        ("The measured height is 42 m.", "stop"),
        ("The measured height is 42 m [SRC-17] [SRC-17].", "stop"),
        ("The measured height is 42 m [SRC-18].", "stop"),
        ("[SRC-17] The measured height is 42 m.", "stop"),
        ("The measured height is 42 [SRC-17].", "stop"),
        ("The measured heights are 42 m and 43 m [SRC-17].", "stop"),
        ("The measured height is 42 m [SRC-17].", "length"),
    ],
)
def test_citation_validator_rejects_broken_claims(battery: Any, content: str, finish_reason: str) -> None:
    completion = battery.SanitizedCompletion(
        content=content,
        latency_sec=0.1,
        prompt_tokens=20,
        completion_tokens=20,
        finish_reason=finish_reason,
        reasoning_present=False,
    )

    assert not battery._citation_is_preserved(completion)


def test_citation_prompt_declares_the_marker_contract_in_system_message(battery: Any) -> None:
    case = next(case for case in battery._live_cases() if case.name == "citation_preservation")
    system = case.messages[0]["content"]

    assert "byte-for-byte exactly once" in system
    assert "immediately after the factual claim" in system
    assert "Never invent, alter, or omit" in system


def test_stop_probe_uses_the_native_return_token_with_a_bounded_generation(battery: Any) -> None:
    case = next(case for case in battery._live_cases() if case.name == "stop_sequence")
    serialized_prompt = json.dumps(case.messages, ensure_ascii=False)

    assert case.max_tokens == 128
    assert case.extra == {"stop": ["<|return|>"]}
    assert serialized_prompt.count("<|return|>") == 0
    assert "alpha" in serialized_prompt


def test_near_limit_probe_requires_one_marker_and_a_natural_stop(battery: Any) -> None:
    context_tokens = 4096
    case = battery._near_limit_long_context_case(context_tokens)
    common = {
        "latency_sec": 0.1,
        "prompt_tokens": context_tokens - 512,
        "completion_tokens": 8,
        "reasoning_present": False,
    }

    assert json.dumps(case.messages, ensure_ascii=False).count(battery._LONG_CONTEXT_MARKER) == 1
    assert case.validator(
        battery.SanitizedCompletion(
            content=f"Memory: {battery._LONG_CONTEXT_MARKER}.",
            finish_reason="stop",
            **common,
        )
    )
    assert not case.validator(
        battery.SanitizedCompletion(
            content=battery._LONG_CONTEXT_MARKER,
            finish_reason="length",
            **common,
        )
    )
    assert not case.validator(
        battery.SanitizedCompletion(
            content=f"{battery._LONG_CONTEXT_MARKER} {battery._LONG_CONTEXT_MARKER}",
            finish_reason="stop",
            **common,
        )
    )


def test_tool_probe_validates_shape_without_executing_or_forwarding_reasoning(
    monkeypatch: pytest.MonkeyPatch, battery: Any
) -> None:
    fake = FakeEndpoint(battery, invalid_tool=True)
    monkeypatch.setattr(battery, "request_json", fake)

    rows = battery._run_tool_protocol(
        base_url="https://192.168.1.35:8443/v1",
        api_key=API_KEY,
        timeout_sec=10.0,
        ca_file=CA_FILE,
    )

    assert [row["status"] for row in rows] == ["failed", "failed"]
    assert [row["case"] for row in rows] == ["tool_call_shape", "tool_result_continuation"]
    assert len(fake.calls) == 1
    assert "Paris" not in json.dumps(rows)
    assert "PRIVATE_TOOL_REASONING" not in json.dumps(rows)


@pytest.mark.parametrize("include_index", [False, True])
def test_tool_probe_accepts_optional_zero_index_and_normalizes_it(
    monkeypatch: pytest.MonkeyPatch,
    battery: Any,
    include_index: bool,
) -> None:
    fake = FakeEndpoint(battery, include_tool_index=include_index)
    monkeypatch.setattr(battery, "request_json", fake)

    observation = battery._tool_call_request(
        base_url="https://192.168.1.35:8443/v1",
        api_key=API_KEY,
        timeout_sec=10.0,
        ca_file=CA_FILE,
    )

    assert set(observation.assistant_message["tool_calls"][0]) == {"id", "type", "function"}


@pytest.mark.parametrize("tool_index", [True, -1, 1, None, "0"])
def test_tool_probe_rejects_noncanonical_optional_index(
    monkeypatch: pytest.MonkeyPatch,
    battery: Any,
    tool_index: object,
) -> None:
    fake = FakeEndpoint(battery, tool_index=tool_index)
    monkeypatch.setattr(battery, "request_json", fake)

    with pytest.raises(battery.EndpointError, match="tool-call index"):
        battery._tool_call_request(
            base_url="https://192.168.1.35:8443/v1",
            api_key=API_KEY,
            timeout_sec=10.0,
            ca_file=CA_FILE,
        )


def test_wrong_inventory_fails_closed_before_any_completion(
    monkeypatch: pytest.MonkeyPatch, battery: Any
) -> None:
    fake = FakeEndpoint(battery, alias_ok=False)
    report = _run_with_fake(monkeypatch, battery, fake)

    assert report["status"] == "failed"
    assert len(fake.calls) == 1
    assert fake.calls[0]["url"].endswith("/v1/models")
    assert next(row for row in report["cases"] if row["case"] == "exact_model_alias")["status"] == "failed"
    assert all(
        row["status"] == "failed"
        for row in report["cases"]
        if row["case"] not in {"reject_empty", "reject_nan", "reject_degeneration", "reject_harmony"}
    )


def test_negative_protocol_cases_are_local_and_content_free(battery: Any) -> None:
    rows = battery._protocol_rejection_rows()

    assert {row["case"] for row in rows} == {
        "reject_empty",
        "reject_nan",
        "reject_degeneration",
        "reject_harmony",
    }
    assert all(row["status"] == "passed" for row in rows)
    assert all(set(row) == battery._EVIDENCE_KEYS for row in rows)
    serialized = json.dumps(rows)
    assert "NaN" not in serialized
    assert "<|analysis|>" not in serialized
    assert "repeat repeat" not in serialized


def test_live_battery_requires_private_ca_https_without_touching_network(battery: Any) -> None:
    with pytest.raises(battery.EndpointError):
        battery.run_battery(
            base_url="http://127.0.0.1:30000/v1",
            api_key=API_KEY,
            timeout_sec=10.0,
            ca_file=CA_FILE,
        )


def test_disconnect_probe_parses_exact_sse_and_requires_bounded_recovery(
    monkeypatch: pytest.MonkeyPatch, battery: Any
) -> None:
    completion_calls: list[float] = []

    def completion(**kwargs: Any) -> Any:
        completion_calls.append(float(kwargs["timeout_sec"]))
        return battery.SanitizedCompletion(
            content="ready",
            latency_sec=0.2,
            prompt_tokens=3,
            completion_tokens=1,
            finish_reason="stop",
            reasoning_present=True,
        )

    class Response:
        status = 200
        headers = object()

        def __init__(self) -> None:
            self.lines = iter(
                [
                    b"\r\n",
                    (
                        b'data: {"model":"friday-secondary-gptoss20b",'
                        b'"choices":[{"index":0,"delta":{"role":"assistant"}}]}\r\n'
                    ),
                ]
            )

        def readline(self, _limit: int) -> bytes:
            return next(self.lines, b"")

    class Connection:
        def __init__(self) -> None:
            self.body = b""
            self.closed = False

        def request(
            self,
            _method: str,
            _path: str,
            *,
            body: bytes,
            headers: dict[str, str],
        ) -> None:
            assert headers["Authorization"] == f"Bearer {API_KEY}"
            self.body = body

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(battery, "_completion_request", completion)
    metric_rows = iter(
        [
            battery.CancellationMetrics(aborted_total=None, running=0.0, queued=0.0),
            battery.CancellationMetrics(aborted_total=None, running=1.0, queued=0.0),
            battery.CancellationMetrics(aborted_total=None, running=0.0, queued=0.0),
        ]
    )
    monkeypatch.setattr(battery, "_cancellation_metrics", lambda **_kwargs: next(metric_rows))
    monkeypatch.setattr(battery, "build_tls_context", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(battery, "validate_profile_headers", lambda _headers: None)
    monkeypatch.setattr(battery.http.client, "HTTPSConnection", lambda *_args, **_kwargs: connection)

    rows = battery._run_disconnect_protocol(
        base_url="https://192.168.1.35:8443/v1",
        api_key=API_KEY,
        timeout_sec=60.0,
        ca_file=CA_FILE,
    )

    assert [row["status"] for row in rows] == ["passed", "passed"]
    assert completion_calls == [60.0, 3.0]
    payload = json.loads(connection.body)
    assert payload["max_tokens"] == payload["min_tokens"] == 2_048
    assert payload["ignore_eos"] is True
    assert payload["stream"] is True
    assert connection.closed is True


def test_cancellation_metrics_reads_only_the_three_bounded_series(
    monkeypatch: pytest.MonkeyPatch, battery: Any
) -> None:
    observed_url = ""
    body = "\n".join(
        [
            "# HELP ignored content-free fixture",
            'sglang:num_aborted_requests_total{model="alias"} 2',
            "sglang:num_running_reqs 0",
            "sglang:num_queue_reqs 0",
            'unrelated_metric{prompt="must-not-be-retained"} 99',
        ]
    )

    def request_text(_method: str, url: str, **_kwargs: Any) -> tuple[str, float]:
        nonlocal observed_url
        observed_url = url
        return body, 0.01

    monkeypatch.setattr(battery, "request_text", request_text)
    snapshot = battery._cancellation_metrics(
        base_url="https://192.168.1.35:8443/v1",
        api_key=API_KEY,
        timeout_sec=3.0,
        ca_file=CA_FILE,
    )

    assert observed_url == "https://192.168.1.35:8443/metrics"
    assert snapshot == battery.CancellationMetrics(aborted_total=2.0, running=0.0, queued=0.0)
    assert "prompt" not in repr(snapshot)
    with pytest.raises(battery.EndpointError):
        battery._metric_values("sglang:num_running_reqs NaN", "sglang:num_running_reqs")


def test_disconnect_probe_requires_a_running_gauge_before_client_close(
    monkeypatch: pytest.MonkeyPatch, battery: Any
) -> None:
    monkeypatch.setattr(
        battery,
        "_completion_request",
        lambda **_kwargs: battery.SanitizedCompletion(
            content="ready",
            latency_sec=0.2,
            prompt_tokens=3,
            completion_tokens=1,
            finish_reason="stop",
            reasoning_present=False,
        ),
    )
    monkeypatch.setattr(
        battery,
        "_cancellation_metrics",
        lambda **_kwargs: battery.CancellationMetrics(
            aborted_total=1.0,
            running=0.0,
            queued=0.0,
        ),
    )

    class Response:
        status = 200
        headers = object()

        def readline(self, _limit: int) -> bytes:
            return b'data: {"model":"friday-secondary-gptoss20b","choices":[{"index":0,"delta":{}}]}\r\n'

    class Connection:
        def request(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            return None

    monotonic_values = iter([0.0, 0.0, 9.0])
    monkeypatch.setattr(battery.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(battery.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(battery, "build_tls_context", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(battery, "validate_profile_headers", lambda _headers: None)
    monkeypatch.setattr(battery.http.client, "HTTPSConnection", lambda *_args, **_kwargs: Connection())

    rows = battery._run_disconnect_protocol(
        base_url="https://192.168.1.35:8443/v1",
        api_key=API_KEY,
        timeout_sec=60.0,
        ca_file=CA_FILE,
    )

    assert [row["status"] for row in rows] == ["failed", "failed"]


def test_cancellation_metrics_allows_the_pinned_runtime_to_omit_abort_counter(
    monkeypatch: pytest.MonkeyPatch, battery: Any
) -> None:
    monkeypatch.setattr(
        battery,
        "request_text",
        lambda *_args, **_kwargs: (
            "sglang:num_running_reqs 1\nsglang:num_queue_reqs 0\n",
            0.01,
        ),
    )

    snapshot = battery._cancellation_metrics(
        base_url="https://192.168.1.35:8443/v1",
        api_key=API_KEY,
        timeout_sec=3.0,
        ca_file=CA_FILE,
    )

    assert snapshot == battery.CancellationMetrics(aborted_total=None, running=1.0, queued=0.0)


@pytest.mark.parametrize(
    "event",
    [
        b"data: garbage\r\n",
        b'data: {"model":"wrong","choices":[{"delta":{}}]}\r\n',
        b"data: [DONE]\r\n",
    ],
)
def test_disconnect_probe_rejects_invalid_or_completed_streams(
    monkeypatch: pytest.MonkeyPatch, battery: Any, event: bytes
) -> None:
    monkeypatch.setattr(
        battery,
        "_completion_request",
        lambda **_kwargs: battery.SanitizedCompletion(
            content="ready",
            latency_sec=0.2,
            prompt_tokens=0,
            completion_tokens=0,
            finish_reason="stop",
            reasoning_present=False,
        ),
    )
    monkeypatch.setattr(
        battery,
        "_cancellation_metrics",
        lambda **_kwargs: battery.CancellationMetrics(
            aborted_total=0.0,
            running=0.0,
            queued=0.0,
        ),
    )

    class Response:
        status = 200
        headers = object()

        def readline(self, _limit: int) -> bytes:
            nonlocal event
            current, event = event, b""
            return current

    class Connection:
        def request(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            return None

    monkeypatch.setattr(battery, "build_tls_context", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(battery.http.client, "HTTPSConnection", lambda *_args, **_kwargs: Connection())

    rows = battery._run_disconnect_protocol(
        base_url="https://192.168.1.35:8443/v1",
        api_key=API_KEY,
        timeout_sec=60.0,
        ca_file=CA_FILE,
    )

    assert [row["status"] for row in rows] == ["failed", "failed"]


def test_disconnect_recovery_cannot_pass_outside_its_strict_budget(
    monkeypatch: pytest.MonkeyPatch, battery: Any
) -> None:
    call_index = 0

    def completion(**_kwargs: Any) -> Any:
        nonlocal call_index
        call_index += 1
        return battery.SanitizedCompletion(
            content="ready",
            latency_sec=0.2 if call_index == 1 else 3.1,
            prompt_tokens=0,
            completion_tokens=0,
            finish_reason="stop",
            reasoning_present=False,
        )

    class Response:
        status = 200
        headers = object()

        def readline(self, _limit: int) -> bytes:
            return b'data: {"model":"friday-secondary-gptoss20b","choices":[{"index":0,"delta":{}}]}\r\n'

    class Connection:
        def request(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            return None

    monkeypatch.setattr(battery, "_completion_request", completion)
    metric_rows = iter(
        [
            battery.CancellationMetrics(aborted_total=None, running=0.0, queued=0.0),
            battery.CancellationMetrics(aborted_total=None, running=1.0, queued=0.0),
            battery.CancellationMetrics(aborted_total=None, running=0.0, queued=0.0),
        ]
    )
    monkeypatch.setattr(battery, "_cancellation_metrics", lambda **_kwargs: next(metric_rows))
    monkeypatch.setattr(battery, "build_tls_context", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(battery, "validate_profile_headers", lambda _headers: None)
    monkeypatch.setattr(battery.http.client, "HTTPSConnection", lambda *_args, **_kwargs: Connection())

    rows = battery._run_disconnect_protocol(
        base_url="https://192.168.1.35:8443/v1",
        api_key=API_KEY,
        timeout_sec=60.0,
        ca_file=CA_FILE,
    )

    assert [row["status"] for row in rows] == ["passed", "failed"]


def test_cli_failure_never_prints_exception_or_credential_path(
    monkeypatch: pytest.MonkeyPatch,
    battery: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_path = tmp_path / "SECRET-API-KEY-PATH"

    def fail_key(_path: Path) -> str:
        raise battery.EndpointError(f"sensitive path: {secret_path}")

    monkeypatch.setattr(battery, "load_api_key", fail_key)
    monkeypatch.setattr(
        battery,
        "configure_expected_model",
        lambda *_paths: battery.EXPECTED_MODEL,
    )
    result = battery.main(
        [
            "--base-url",
            "https://192.168.1.35:8443/v1",
            "--api-key-file",
            str(secret_path),
            "--ca-file",
            str(tmp_path / "ca.crt"),
            "--profile-manifest",
            str(tmp_path / "profile.json"),
            "--output",
            str(tmp_path / "evidence.json"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert captured.err == "quality battery failed: closed_error\n"
    assert str(secret_path) not in captured.err
