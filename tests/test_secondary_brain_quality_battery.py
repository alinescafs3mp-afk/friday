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
        alias_ok: bool = True,
    ) -> None:
        self.battery = battery
        self.overrides = dict(overrides or {})
        self.invalid_tool = invalid_tool
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
        messages = payload.get("messages")
        assert isinstance(messages, list)
        if any(isinstance(message, dict) and message.get("role") == "tool" for message in messages):
            return self._completion("17", reasoning="PRIVATE_CONTINUATION_REASONING")
        if isinstance(payload.get("tool_choice"), dict):
            city = "Paris" if self.invalid_tool else "Moscow"
            return (
                {
                    "model": self.battery.EXPECTED_MODEL,
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "reasoning_content": "PRIVATE_TOOL_REASONING",
                                "tool_calls": [
                                    {
                                        "id": "call-safe-17",
                                        "type": "function",
                                        "function": {
                                            "name": "lookup_temperature",
                                            "arguments": json.dumps({"city": city}),
                                        },
                                    }
                                ],
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
            battery.CancellationMetrics(aborted_total=4.0, running=0.0, queued=0.0),
            battery.CancellationMetrics(aborted_total=5.0, running=0.0, queued=0.0),
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
            battery.CancellationMetrics(aborted_total=7.0, running=0.0, queued=0.0),
            battery.CancellationMetrics(aborted_total=8.0, running=0.0, queued=0.0),
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
