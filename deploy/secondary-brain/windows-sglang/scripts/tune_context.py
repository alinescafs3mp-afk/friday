"""Measure a context ladder against one explicitly configured SGLang candidate."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from endpoint_common import (
    EndpointError,
    atomic_write_json,
    configure_expected_model,
    load_api_key,
    normalize_base_url,
    stream_chat_completion,
    verify_remote_profile_epoch,
)
from gpu_telemetry import GpuSampler, GpuTelemetryError, sample_summary

_LADDER = (4096, 8192, 12288, 16384, 24576, 32768)
_MEMORY_GRID = (0.86, 0.88, 0.90, 0.92)


def _context_prompt(target_tokens: int) -> list[dict[str, str]]:
    repeats = max(1, target_tokens - 256)
    body = "probe " * repeats
    return [
        {
            "role": "system",
            "content": (
                "This is a numerical capacity canary. Do not quote the input. "
                "Return at least 256 short numbered words in the final channel."
            ),
        },
        {"role": "user", "content": body + "\nBegin the numbered final answer now."},
    ]


def _trial(
    *,
    base_url: str,
    api_key: str,
    context_tokens: int,
    repeat: int,
    timeout_sec: float,
    generation_tokens: int,
    ca_file: Path | None,
) -> dict[str, Any]:
    with GpuSampler() as sampler:
        completion = stream_chat_completion(
            base_url,
            api_key=api_key,
            messages=_context_prompt(context_tokens),
            timeout_sec=timeout_sec,
            max_tokens=generation_tokens,
            ca_file=ca_file,
        )
    if sampler.error is not None:
        raise sampler.error
    gpu = sample_summary(sampler.samples)
    value = completion.completion
    prompt_near_limit = int(context_tokens * 0.80) <= value.prompt_tokens <= context_tokens
    generated_envelope_met = value.completion_tokens >= 256
    required_headroom_mib = max(512.0, gpu["total_mib"] * 0.05)
    headroom_met = gpu["minimum_free_mib"] >= required_headroom_mib
    return {
        "repeat": repeat,
        "context_target_tokens": context_tokens,
        "prompt_tokens": value.prompt_tokens,
        "completion_tokens": value.completion_tokens,
        "ttft_sec": round(completion.ttft_sec, 6),
        "end_to_end_sec": round(value.latency_sec, 6),
        "decode_tokens_per_sec_after_first_token": round(
            value.completion_tokens / max(0.001, value.latency_sec - completion.ttft_sec), 6
        ),
        "finish_reason": value.finish_reason,
        "reasoning_field_present": value.reasoning_present,
        "prompt_near_limit": prompt_near_limit,
        "generated_envelope_met": generated_envelope_met,
        "required_headroom_mib": round(required_headroom_mib, 3),
        "headroom_met": headroom_met,
        "gpu": {key: round(number, 3) for key, number in gpu.items()},
        "raw_prompt_retained": False,
        "raw_response_retained": False,
    }


def run_ladder(
    *,
    base_url: str,
    api_key: str,
    candidates: tuple[int, ...],
    repeats: int,
    timeout_sec: float,
    generation_tokens: int,
    mem_fraction_static: float,
    ca_file: Path | None,
) -> dict[str, Any]:
    trials: list[dict[str, Any]] = []
    admitted: list[int] = []
    for candidate in candidates:
        candidate_trials = [
            _trial(
                base_url=base_url,
                api_key=api_key,
                context_tokens=candidate,
                repeat=index + 1,
                timeout_sec=timeout_sec,
                generation_tokens=generation_tokens,
                ca_file=ca_file,
            )
            for index in range(repeats)
        ]
        trials.extend(candidate_trials)
        passed = all(
            trial["prompt_near_limit"] and trial["generated_envelope_met"] and trial["headroom_met"]
            for trial in candidate_trials
        )
        if passed:
            admitted.append(candidate)
        else:
            break
    return {
        "schema": "friday.secondary-context-capacity-trial.v1",
        "status": "measured_not_yet_certified",
        "observed_at": datetime.now(UTC).isoformat(),
        "mem_fraction_static": mem_fraction_static,
        "candidates": list(candidates),
        "repeats_per_candidate": repeats,
        "generation_tokens": generation_tokens,
        "largest_passing_trial_tokens": max(admitted, default=0),
        "trial_count": len(trials),
        "median_ttft_sec": round(statistics.median(trial["ttft_sec"] for trial in trials), 6),
        "trials": trials,
        "cold_restart_retest_required": True,
        "thermal_soak_required": True,
        "capacity_manifest_emitted": False,
        "note": "Repeat the winning size after cold restart and in soak before accepting a cap.",
    }


def _parse_candidates(value: str) -> tuple[int, ...]:
    try:
        candidates = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("candidates must be comma-separated integers") from exc
    if not candidates or candidates != tuple(sorted(set(candidates))):
        raise argparse.ArgumentTypeError("candidates must be unique and strictly ascending")
    if any(candidate not in _LADDER for candidate in candidates):
        raise argparse.ArgumentTypeError("candidate is outside the approved initial ladder")
    return candidates


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:30000/v1")
    parser.add_argument("--api-key-file", required=True, type=Path)
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--profile-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--candidates", default=_LADDER, type=_parse_candidates)
    parser.add_argument("--repeats", default=3, type=int, choices=range(3, 11))
    parser.add_argument("--timeout-sec", default=180.0, type=float)
    parser.add_argument("--generation-tokens", default=320, type=int, choices=range(256, 513))
    parser.add_argument("--mem-fraction-static", required=True, type=float, choices=_MEMORY_GRID)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        configure_expected_model(args.profile_manifest, args.ca_file)
        api_key = load_api_key(args.api_key_file)
        if normalize_base_url(args.base_url).startswith("https://"):
            if args.ca_file is None:
                raise EndpointError("HTTPS capacity trial requires an explicit CA file")
            verify_remote_profile_epoch(
                args.base_url,
                api_key=api_key,
                timeout_sec=args.timeout_sec,
                ca_file=args.ca_file,
            )
        report = run_ladder(
            base_url=args.base_url,
            api_key=api_key,
            candidates=args.candidates,
            repeats=args.repeats,
            timeout_sec=args.timeout_sec,
            generation_tokens=args.generation_tokens,
            mem_fraction_static=args.mem_fraction_static,
            ca_file=args.ca_file,
        )
        atomic_write_json(args.output, report)
        print(json.dumps({key: value for key, value in report.items() if key != "trials"}, sort_keys=True))
        return 0 if report["largest_passing_trial_tokens"] else 2
    except (EndpointError, GpuTelemetryError) as exc:
        print(f"capacity trial failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
