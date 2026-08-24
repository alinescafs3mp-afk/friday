"""Run a secret-free, single-request mixed SGLang thermal and quality soak."""

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
    normalize_base_url,
    runtime_process_epoch,
    verify_remote_profile_epoch,
)
from gpu_telemetry import GpuSampler, GpuTelemetryError, sample_summary


@dataclass(frozen=True, slots=True)
class SoakCase:
    name: str
    prompt: str
    validator: Callable[[str], bool]
    extra: dict[str, object] | None = None


def _has_cyrillic(value: str) -> bool:
    return any("а" <= char.casefold() <= "я" or char.casefold() == "ё" for char in value)


def _has_english(value: str) -> bool:
    return any("a" <= char.casefold() <= "z" for char in value)


def _is_exact_extraction(value: str) -> bool:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return False
    return parsed == {"amount": 17, "date": "2026-08-24", "person": "Артемьев"}


def _cases() -> tuple[SoakCase, ...]:
    return (
        SoakCase("russian", "Одним предложением объясни, зачем проверяют резервный узел.", _has_cyrillic),
        SoakCase("english", "In one sentence, explain why an optional node must fail soft.", _has_english),
        SoakCase(
            "arithmetic",
            "Return only the decimal result of (19 * 3) - 15.",
            lambda value: value.strip() == "42",
        ),
        SoakCase(
            "json_extraction",
            (
                "Return only JSON with keys person, date, amount from: "
                "Артемьев, 24.08.2026, сумма 17. Use ISO date and numeric amount."
            ),
            _is_exact_extraction,
            {"response_format": {"type": "json_object"}},
        ),
        SoakCase(
            "unicode",
            "Повтори без изменений только это имя файла: Проекты/Ёж №17 — финал.txt",
            lambda value: "Проекты/Ёж №17 — финал.txt" in value,
        ),
        SoakCase(
            "contradiction",
            "Ответь одним словом да или нет: утверждения «A больше B» и «A меньше B» противоречат?",
            lambda value: _has_cyrillic(value) and len(value) <= 64,
        ),
    )


def _safe_trial(case: SoakCase, completion: SanitizedCompletion, sequence: int) -> dict[str, object]:
    return {
        "sequence": sequence,
        "case": case.name,
        "passed": case.validator(completion.content),
        "latency_sec": round(completion.latency_sec, 6),
        "prompt_tokens": completion.prompt_tokens,
        "completion_tokens": completion.completion_tokens,
        "finish_reason": completion.finish_reason,
        "reasoning_field_present": completion.reasoning_present,
        "raw_response_retained": False,
    }


def run_soak(
    *,
    base_url: str,
    api_key: str,
    duration_sec: int,
    minimum_requests: int,
    timeout_sec: float,
    maximum_temperature_c: float,
    checkpoint: Path,
    ca_file: Path | None,
) -> dict[str, object]:
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
                            "content": "Return final content only. Never reveal internal reasoning or call tools.",
                        },
                        {"role": "user", "content": case.prompt},
                    ],
                    timeout_sec=timeout_sec,
                    max_tokens=128,
                    temperature=0.0,
                    extra=case.extra,
                    ca_file=ca_file,
                )
                trial = _safe_trial(case, completion, len(trials) + 1)
                if not trial["passed"]:
                    failures += 1
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
            trials.append(trial)
            if len(trials) % 10 == 0:
                atomic_write_json(
                    checkpoint,
                    {
                        "schema": "friday.secondary-soak-checkpoint.v1",
                        "status": "running",
                        "completed_requests": len(trials),
                        "failures": failures,
                        "elapsed_sec": round(time.monotonic() - started, 3),
                        "raw_content_retained": False,
                    },
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
    parser.add_argument("--base-url", default="http://127.0.0.1:30000/v1")
    parser.add_argument("--api-key-file", required=True, type=Path)
    parser.add_argument("--ca-file", type=Path)
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
        if normalize_base_url(args.base_url).startswith("https://"):
            if args.ca_file is None:
                raise EndpointError("HTTPS soak requires an explicit CA file")
            verify_remote_profile_epoch(
                args.base_url,
                api_key=api_key,
                timeout_sec=args.timeout_sec,
                ca_file=args.ca_file,
            )
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
        atomic_write_json(args.output, report)
        summary = {key: value for key, value in report.items() if key != "trials"}
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0 if report["status"] == "passed" else 2
    except (EndpointError, GpuTelemetryError, ValueError) as exc:
        message = re.sub(r"[^a-zA-Z0-9 _.-]", "", str(exc))[:160]
        print(f"soak failed: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
