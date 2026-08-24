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


def test_complete_trial_passes_and_json_grammar_request_remains_enabled(soak: Any) -> None:
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
    json_case = next(case for case in soak._cases() if case.name == "json_extraction")
    assert json_case.extra == {"response_format": {"type": "json_object"}}


@pytest.mark.parametrize("value", ["42", "42.0", "42.000"])
def test_arithmetic_case_accepts_equivalent_integer_renderings(soak: Any, value: str) -> None:
    assert soak._is_integer_42(value)


@pytest.mark.parametrize("value", ["42.5", "The answer is 42", "41", ""])
def test_arithmetic_case_rejects_wrong_or_verbose_results(soak: Any, value: str) -> None:
    assert not soak._is_integer_42(value)
