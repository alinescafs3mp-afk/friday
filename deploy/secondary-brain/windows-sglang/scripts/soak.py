"""Run a secret-free, single-request mixed SGLang thermal and protocol soak."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from endpoint_common import (
    EndpointError,
    SanitizedCompletion,
    atomic_write_json,
    chat_completion,
    configure_expected_model,
    evidence_identity,
    load_api_key,
    runtime_process_epoch,
    verify_remote_profile_epoch,
    write_new_json,
)
from gpu_telemetry import GpuSampler, GpuTelemetryError, sample_summary


@dataclass(frozen=True, slots=True)
class SoakCase:
    name: str
    prompt: str
    validator: Callable[[str], bool]
    max_tokens: int = 256
    reasoning_effort: str = "low"
    extra: dict[str, object] | None = None


def _load_unique_json_object(value: str) -> dict[str, object] | None:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        parsed: dict[str, object] = {}
        for key, item in pairs:
            if key in parsed:
                raise ValueError("duplicate JSON key")
            parsed[key] = item
        return parsed

    try:
        parsed = json.loads(value, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_exact_extraction(value: str) -> bool:
    parsed = _load_unique_json_object(value)
    return parsed == {"amount": 17, "date": "2026-08-24", "person": "Ada"} and type(parsed["amount"]) is int


def _is_exact_text(value: str, expected: str) -> bool:
    parsed = _load_unique_json_object(value)
    return parsed == {"text": expected}


def _is_integer_42(value: str) -> bool:
    parsed = _load_unique_json_object(value)
    return parsed == {"result": 42} and type(parsed["result"]) is int


def _is_exact_unicode_filename(value: str) -> bool:
    parsed = _load_unique_json_object(value)
    return parsed == {"filename": "Проекты/Ёж №17 — финал.txt"}


def _is_exact_contradiction(value: str) -> bool:
    parsed = _load_unique_json_object(value)
    return parsed == {"verdict": "CONTRADICTION"}


def _single_value_object_response_format(
    *,
    name: str,
    property_name: str,
    expected: str | int,
) -> dict[str, object]:
    if isinstance(expected, bool) or not isinstance(expected, (str, int)):
        raise ValueError("single-value schema requires a string or integer")
    property_type = "integer" if type(expected) is int else "string"
    return {
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": name,
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        property_name: {
                            "type": property_type,
                            "enum": [expected],
                        }
                    },
                    "required": [property_name],
                    "additionalProperties": False,
                },
            },
        }
    }


def _exact_extraction_response_format() -> dict[str, object]:
    return {
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


def _exact_unicode_response_format() -> dict[str, object]:
    return _single_value_object_response_format(
        name="exact_unicode_filename",
        property_name="filename",
        expected="Проекты/Ёж №17 — финал.txt",
    )


def _cases() -> tuple[SoakCase, ...]:
    # The mandatory quality battery independently exercises these abilities
    # without singleton-value grammars.  This long run instead proves sustained
    # endpoint, grammar, sampler, GPU and thermal stability with zero drift.
    return (
        SoakCase(
            "russian",
            'Верни ровно JSON: {"text":"Резервный узел готов."}',
            lambda value: _is_exact_text(value, "Резервный узел готов."),
            extra=_single_value_object_response_format(
                name="exact_russian_text",
                property_name="text",
                expected="Резервный узел готов.",
            ),
        ),
        SoakCase(
            "english",
            'Return exactly this JSON: {"text":"Node ready."}',
            lambda value: _is_exact_text(value, "Node ready."),
            extra=_single_value_object_response_format(
                name="exact_english_text",
                property_name="text",
                expected="Node ready.",
            ),
        ),
        SoakCase(
            "arithmetic",
            'Calculate (19 * 3) - 15 and return only JSON with integer key "result".',
            _is_integer_42,
            max_tokens=512,
            reasoning_effort="medium",
            extra=_single_value_object_response_format(
                name="exact_arithmetic_result",
                property_name="result",
                expected=42,
            ),
        ),
        SoakCase(
            "json_extraction",
            (
                "Return only JSON with keys person, date, amount from: "
                "Ada, 24.08.2026, amount 17. Use ISO date and numeric amount."
            ),
            _is_exact_extraction,
            extra=_exact_extraction_response_format(),
        ),
        SoakCase(
            "unicode",
            'Return only JSON in this exact form: {"filename":"Проекты/Ёж №17 — финал.txt"}',
            _is_exact_unicode_filename,
            # ``json_object`` constrains only the shape and still permits rare
            # Cyrillic value drift. The admitted SGLang endpoint supports an
            # enum grammar, so the wire contract enforces the exact value too.
            extra=_exact_unicode_response_format(),
        ),
        SoakCase(
            "contradiction",
            (
                "Statements A>B and A<B describe the same A and B at the same time. "
                'Return {"verdict":"CONTRADICTION"} if they conflict, otherwise return '
                '{"verdict":"CONSISTENT"}.'
            ),
            _is_exact_contradiction,
            # The unconstrained semantic classification remains in the quality
            # battery. This long thermal/protocol soak constrains the known
            # answer so rare sampler spelling drift cannot impersonate an
            # endpoint outage after thousands of otherwise valid requests.
            extra=_single_value_object_response_format(
                name="exact_contradiction_verdict",
                property_name="verdict",
                expected="CONTRADICTION",
            ),
        ),
    )


def _safe_trial(case: SoakCase, completion: SanitizedCompletion, sequence: int) -> dict[str, object]:
    passed = completion.finish_reason == "stop" and case.validator(completion.content)
    trial: dict[str, object] = {
        "sequence": sequence,
        "case": case.name,
        "passed": passed,
        "latency_sec": round(completion.latency_sec, 6),
        "prompt_tokens": completion.prompt_tokens,
        "completion_tokens": completion.completion_tokens,
        "finish_reason": completion.finish_reason,
        "reasoning_field_present": completion.reasoning_present,
        "raw_response_retained": False,
    }
    if not passed:
        trial["failure_class"] = (
            "finish_reason" if completion.finish_reason != "stop" else "contract_mismatch"
        )
    return trial


def _checkpoint_evidence(
    *,
    completed_requests: int,
    failures: int,
    elapsed_sec: float,
    failures_by_case: dict[str, int],
    last_failure: dict[str, object] | None,
) -> dict[str, object]:
    evidence: dict[str, object] = {
        "schema": "friday.secondary-soak-checkpoint.v1",
        "status": "running",
        "completed_requests": completed_requests,
        "failures": failures,
        "failures_by_case": {name: count for name, count in sorted(failures_by_case.items()) if count > 0},
        "elapsed_sec": round(elapsed_sec, 3),
        "raw_content_retained": False,
    }
    if last_failure is not None:
        evidence["last_failure"] = {
            "sequence": last_failure["sequence"],
            "case": last_failure["case"],
            "failure_class": last_failure["failure_class"],
        }
    return evidence


def run_soak(
    *,
    base_url: str,
    api_key: str,
    duration_sec: int,
    minimum_requests: int,
    timeout_sec: float,
    maximum_temperature_c: float,
    checkpoint: Path,
    ca_file: Path,
) -> dict[str, object]:
    verify_remote_profile_epoch(
        base_url,
        api_key=api_key,
        timeout_sec=min(timeout_sec, 10.0),
        ca_file=ca_file,
    )
    cases = _cases()
    runtime_epoch = runtime_process_epoch(
        base_url,
        api_key=api_key,
        timeout_sec=min(timeout_sec, 10.0),
        ca_file=ca_file,
    )
    started = time.monotonic()
    trials: list[dict[str, object]] = []
    failures = 0
    failures_by_case = {case.name: 0 for case in cases}
    last_failure: dict[str, object] | None = None
    consecutive_failures = 0
    with GpuSampler(interval_sec=0.5) as sampler:
        while time.monotonic() - started < duration_sec or len(trials) < minimum_requests:
            case = cases[len(trials) % len(cases)]
            try:
                completion = chat_completion(
                    base_url,
                    api_key=api_key,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Follow the user instruction exactly. Return final content only. "
                                "Never reveal internal reasoning or call tools."
                            ),
                        },
                        {"role": "user", "content": case.prompt},
                    ],
                    timeout_sec=timeout_sec,
                    max_tokens=case.max_tokens,
                    temperature=1.0,
                    extra={
                        "reasoning_effort": case.reasoning_effort,
                        "top_p": 1.0,
                        "seed": 0,
                        **(case.extra or {}),
                    },
                    ca_file=ca_file,
                )
                trial = _safe_trial(case, completion, len(trials) + 1)
                if not trial["passed"]:
                    failures += 1
                    failures_by_case[case.name] += 1
                    last_failure = trial
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0
            except EndpointError:
                failures += 1
                consecutive_failures += 1
                trial = {
                    "sequence": len(trials) + 1,
                    "case": case.name,
                    "passed": False,
                    "failure_class": "endpoint_or_protocol_rejection",
                    "raw_response_retained": False,
                }
                failures_by_case[case.name] += 1
                last_failure = trial
            trials.append(trial)
            if not trial["passed"] or len(trials) % 10 == 0:
                atomic_write_json(
                    checkpoint,
                    _checkpoint_evidence(
                        completed_requests=len(trials),
                        failures=failures,
                        elapsed_sec=time.monotonic() - started,
                        failures_by_case=failures_by_case,
                        last_failure=last_failure,
                    ),
                )
            if consecutive_failures >= 5:
                break
    elapsed = time.monotonic() - started
    if sampler.error is not None:
        raise sampler.error
    if (
        runtime_process_epoch(
            base_url,
            api_key=api_key,
            timeout_sec=min(timeout_sec, 10.0),
            ca_file=ca_file,
        )
        != runtime_epoch
    ):
        raise EndpointError("runtime restarted during the soak")
    gpu = sample_summary(sampler.samples)
    required_headroom_mib = max(512.0, gpu["total_mib"] * 0.05)
    passed = (
        elapsed >= duration_sec
        and len(trials) >= minimum_requests
        and failures == 0
        and gpu["minimum_free_mib"] >= required_headroom_mib
        and gpu["peak_temperature_c"] <= maximum_temperature_c
    )
    return {
        "schema": "friday.secondary-sglang-soak.v1",
        "status": "passed" if passed else "failed",
        **evidence_identity(),
        "runtime_process_start_time_seconds": runtime_epoch,
        "observed_at": datetime.now(UTC).isoformat(),
        "duration_required_sec": duration_sec,
        "elapsed_sec": round(elapsed, 3),
        "minimum_requests": minimum_requests,
        "completed_requests": len(trials),
        "failures": failures,
        "failures_by_case": {name: count for name, count in sorted(failures_by_case.items()) if count > 0},
        "last_failure": (
            {
                "sequence": last_failure["sequence"],
                "case": last_failure["case"],
                "failure_class": last_failure["failure_class"],
            }
            if last_failure is not None
            else None
        ),
        "maximum_temperature_c": maximum_temperature_c,
        "required_headroom_mib": round(required_headroom_mib, 3),
        "gpu": {key: round(value, 3) for key, value in gpu.items()},
        "trials": trials,
        "raw_content_retained": False,
        "api_key_retained": False,
    }


def _duration(value: str) -> int:
    parsed = int(value)
    if not 1800 <= parsed <= 3600:
        raise argparse.ArgumentTypeError("duration must be 1800..3600 seconds")
    return parsed


def _minimum_requests(value: str) -> int:
    parsed = int(value)
    if not 100 <= parsed <= 1000:
        raise argparse.ArgumentTypeError("minimum requests must be 100..1000")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key-file", required=True, type=Path)
    parser.add_argument("--ca-file", required=True, type=Path)
    parser.add_argument("--profile-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--duration-sec", default=1800, type=_duration)
    parser.add_argument("--minimum-requests", default=100, type=_minimum_requests)
    parser.add_argument("--timeout-sec", default=60.0, type=float)
    parser.add_argument("--maximum-temperature-c", default=87.0, type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    checkpoint = args.output.with_suffix(args.output.suffix + ".checkpoint")
    try:
        configure_expected_model(args.profile_manifest, args.ca_file)
        api_key = load_api_key(args.api_key_file)
        report = run_soak(
            base_url=args.base_url,
            api_key=api_key,
            duration_sec=args.duration_sec,
            minimum_requests=args.minimum_requests,
            timeout_sec=args.timeout_sec,
            maximum_temperature_c=args.maximum_temperature_c,
            checkpoint=checkpoint,
            ca_file=args.ca_file,
        )
        write_new_json(args.output, report)
        summary = {key: value for key, value in report.items() if key != "trials"}
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0 if report["status"] == "passed" else 2
    except (EndpointError, GpuTelemetryError, ValueError) as exc:
        message = re.sub(r"[^a-zA-Z0-9 _.-]", "", str(exc))[:160]
        print(f"soak failed: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
