"""Closed quality contracts for the secondary SGLang soak."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "deploy" / "secondary-brain" / "windows-sglang" / "scripts"


@pytest.fixture(scope="module", autouse=True)
def _script_import_path() -> Iterator[None]:
    sys.path.insert(0, str(SCRIPTS))
    try:
        yield
    finally:
        sys.path.remove(str(SCRIPTS))


@pytest.fixture(scope="module")
def soak() -> Any:
    return importlib.import_module("soak")


def test_semantically_valid_truncated_trial_fails_closed(soak: Any) -> None:
    case = soak.SoakCase("exact", "unused", lambda value: value == "42")
    completion = soak.SanitizedCompletion(
        content="42",
        latency_sec=0.1,
        prompt_tokens=10,
        completion_tokens=2,
        finish_reason="length",
        reasoning_present=False,
    )

    trial = soak._safe_trial(case, completion, 1)

    assert trial["passed"] is False
    assert trial["finish_reason"] == "length"
    assert trial["failure_class"] == "finish_reason"


def test_complete_trial_passes_and_every_soak_case_uses_an_exact_schema(soak: Any) -> None:
    case = soak.SoakCase("exact", "unused", lambda value: value == "42")
    completion = soak.SanitizedCompletion(
        content="42",
        latency_sec=0.1,
        prompt_tokens=10,
        completion_tokens=2,
        finish_reason="stop",
        reasoning_present=False,
    )

    assert soak._safe_trial(case, completion, 1)["passed"] is True
    schema_names: list[str] = []
    for mixed_case in soak._cases():
        assert mixed_case.extra is not None
        response_format = mixed_case.extra["response_format"]
        assert response_format["type"] == "json_schema"
        json_schema = response_format["json_schema"]
        assert json_schema["strict"] is True
        assert json_schema["schema"]["additionalProperties"] is False
        schema_names.append(json_schema["name"])
    assert len(schema_names) == len(set(schema_names)) == 6


def test_mixed_soak_cases_use_closed_stable_contracts(soak: Any) -> None:
    cases = {case.name: case for case in soak._cases()}

    assert cases["russian"].validator('{"text":"Резервный узел готов."}')
    assert not cases["russian"].validator('{"text":"Резервный узел готов"}')
    assert cases["english"].validator('{"text":"Node ready."}')
    assert not cases["english"].validator('{"text":"Node ready"}')
    assert cases["contradiction"].validator('{"verdict":"CONTRADICTION"}')
    assert not cases["contradiction"].validator('{"verdict":"CONSISTENT"}')
    assert cases["arithmetic"].max_tokens == 512
    assert cases["arithmetic"].reasoning_effort == "medium"
    assert cases["unicode"].extra == {
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "exact_unicode_filename",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "enum": ["Проекты/Ёж №17 — финал.txt"],
                        }
                    },
                    "required": ["filename"],
                    "additionalProperties": False,
                },
            },
        }
    }
    assert cases["unicode"].validator('{"filename":"Проекты/Ёж №17 — финал.txt"}')
    assert cases["json_extraction"].validator('{"amount":17,"date":"2026-08-24","person":"Ada"}')
    assert cases["contradiction"].extra == {
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "exact_contradiction_verdict",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "verdict": {
                            "type": "string",
                            "enum": ["CONTRADICTION"],
                        }
                    },
                    "required": ["verdict"],
                    "additionalProperties": False,
                },
            },
        }
    }
    assert cases["arithmetic"].extra == {
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "exact_arithmetic_result",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"result": {"type": "integer", "enum": [42]}},
                    "required": ["result"],
                    "additionalProperties": False,
                },
            },
        }
    }
    assert cases["json_extraction"].extra == {
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "exact_extraction",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "amount": {"type": "integer", "enum": [17]},
                        "date": {"type": "string", "enum": ["2026-08-24"]},
                        "person": {"type": "string", "enum": ["Ada"]},
                    },
                    "required": ["amount", "date", "person"],
                    "additionalProperties": False,
                },
            },
        }
    }


@pytest.mark.parametrize(
    "value",
    [
        '{"amount":17,"date":"24.08.2026","person":"Ada"}',
        '{"amount":"17","date":"2026-08-24","person":"Ada"}',
        '{"amount":17.0,"date":"2026-08-24","person":"Ada"}',
        '{"amount":true,"date":"2026-08-24","person":"Ada"}',
        '{"amount":0,"amount":17,"date":"2026-08-24","person":"Ada"}',
        '{"amount":17,"date":"2026-08-24","person":"Артемьев"}',
        '{"amount":17,"date":"2026-08-24","person":"Ada","extra":true}',
        "not-json",
    ],
)
def test_extraction_case_rejects_non_exact_or_non_json_outputs(soak: Any, value: str) -> None:
    assert not soak._is_exact_extraction(value)


@pytest.mark.parametrize(
    "value",
    [
        "Проекты/Ёж №17 — финал.txt",
        '{"filename":"Проекты/Ёж №17\u202f—\u202fфинал.txt"}',
        '{"filename":"Проекты/Еж №17 — финал.txt"}',
        '{"filename":"Проекты/Ёж №18 — финал.txt"}',
        '{"filename":"Проекты/Ёж №17 — финал.txt","extra":true}',
        '{"filename":"wrong","filename":"Проекты/Ёж №17 — финал.txt"}',
        "not-json",
    ],
)
def test_unicode_case_rejects_non_exact_or_non_json_outputs(soak: Any, value: str) -> None:
    assert not soak._is_exact_unicode_filename(value)


@pytest.mark.parametrize("value", ['{"result":42}', '{"result": 42}'])
def test_arithmetic_case_accepts_equivalent_integer_renderings(soak: Any, value: str) -> None:
    assert soak._is_integer_42(value)


@pytest.mark.parametrize(
    "value",
    [
        "42",
        '{"result":42.0}',
        '{"result":true}',
        '{"result":"42"}',
        '{"result":41}',
        '{"result":42,"extra":true}',
        '{"result":0,"result":42}',
        "",
    ],
)
def test_arithmetic_case_rejects_wrong_or_verbose_results(soak: Any, value: str) -> None:
    assert not soak._is_integer_42(value)


def test_failed_trials_and_checkpoints_expose_only_closed_diagnostics(soak: Any) -> None:
    case = soak.SoakCase("exact", "unused", lambda value: value == "ok")
    mismatch = soak.SanitizedCompletion(
        content="wrong secret-free test output",
        latency_sec=0.1,
        prompt_tokens=10,
        completion_tokens=2,
        finish_reason="stop",
        reasoning_present=False,
    )
    trial = soak._safe_trial(case, mismatch, 17)

    assert trial["failure_class"] == "contract_mismatch"
    assert "content" not in trial
    checkpoint = soak._checkpoint_evidence(
        completed_requests=20,
        failures=1,
        elapsed_sec=1.23456,
        failures_by_case={"exact": 1, "unused": 0},
        last_failure=trial,
    )
    assert checkpoint["failures_by_case"] == {"exact": 1}
    assert checkpoint["last_failure"] == {
        "sequence": 17,
        "case": "exact",
        "failure_class": "contract_mismatch",
    }
    assert "content" not in checkpoint["last_failure"]


@pytest.mark.parametrize(
    ("failure_mode", "expected_failure_class"),
    [
        ("endpoint", "endpoint_or_protocol_rejection"),
        ("contract", "contract_mismatch"),
    ],
)
def test_run_soak_accounts_for_each_failure_and_checkpoints_immediately(
    soak: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_mode: str,
    expected_failure_class: str,
) -> None:
    class FakeSampler:
        error = None
        samples: list[object] = []

        def __enter__(self) -> FakeSampler:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def one_completion(*_args: object, **_kwargs: object) -> Any:
        if failure_mode == "endpoint":
            raise soak.EndpointError("closed test failure")
        return soak.SanitizedCompletion(
            content='{"text":"wrong"}',
            latency_sec=0.1,
            prompt_tokens=10,
            completion_tokens=2,
            finish_reason="stop",
            reasoning_present=False,
        )

    checkpoints: list[dict[str, object]] = []
    monkeypatch.setattr(soak, "verify_remote_profile_epoch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(soak, "runtime_process_epoch", lambda *_args, **_kwargs: "123")
    monkeypatch.setattr(soak, "chat_completion", one_completion)
    monkeypatch.setattr(soak, "GpuSampler", lambda **_kwargs: FakeSampler())
    monkeypatch.setattr(
        soak,
        "sample_summary",
        lambda _samples: {
            "total_mib": 10_000.0,
            "minimum_free_mib": 1_000.0,
            "peak_temperature_c": 50.0,
        },
    )
    monkeypatch.setattr(
        soak,
        "evidence_identity",
        lambda: {"candidate_profile_id": "test", "candidate_profile_sha256": "0" * 64},
    )
    monkeypatch.setattr(soak, "atomic_write_json", lambda _path, value: checkpoints.append(value))

    report = soak.run_soak(
        base_url="https://127.0.0.1/v1",
        api_key="not-retained",
        duration_sec=0,
        minimum_requests=1,
        timeout_sec=1.0,
        maximum_temperature_c=87.0,
        checkpoint=tmp_path / "checkpoint.json",
        ca_file=tmp_path / "ca.crt",
    )

    assert report["status"] == "failed"
    assert report["completed_requests"] == 1
    assert report["failures"] == 1
    assert report["failures_by_case"] == {"russian": 1}
    assert report["last_failure"] == {
        "sequence": 1,
        "case": "russian",
        "failure_class": expected_failure_class,
    }
    assert len(checkpoints) == 1
    assert checkpoints[0]["last_failure"] == report["last_failure"]


def test_single_value_schema_rejects_boolean_expected_value(soak: Any) -> None:
    with pytest.raises(ValueError, match="string or integer"):
        soak._single_value_object_response_format(
            name="invalid",
            property_name="value",
            expected=True,
        )
