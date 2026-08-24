"""Measure a context ladder against one explicitly configured SGLang candidate."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from endpoint_common import (
    EndpointError,
    configure_expected_model,
    configured_profile_context_tokens,
    configured_profile_mem_fraction_static,
    evidence_identity,
    load_api_key,
    runtime_process_epoch,
    stream_chat_completion,
    verify_remote_profile_epoch,
    write_new_json,
)
from gpu_telemetry import GpuSampler, GpuTelemetryError, expected_gpu_identity, sample_summary

_LADDER = (4096, 8192, 12288, 16384, 24576, 32768, 40960, 49152, 65536)
_MEMORY_GRID = (0.86, 0.88, 0.90, 0.92, 0.94, 0.95, 0.96, 0.97)
_PROTOCOL_TOKEN_RESERVE = 384
_MINIMUM_PROMPT_FRACTION = 0.80


def _context_prompt(
    target_tokens: int,
    generation_tokens: int,
    *,
    repeat: int = 1,
) -> list[dict[str, str]]:
    repeats = target_tokens - generation_tokens - _PROTOCOL_TOKEN_RESERVE
    if repeats < 1:
        raise ValueError("context target cannot reserve generation and protocol tokens")
    if not 1 <= repeat <= 10:
        raise ValueError("capacity repeat is outside the certified bound")
    # Fully identical near-limit requests become almost complete radix-cache
    # hits after the first pass.  That no longer measures a real prefill and can
    # exercise a pinned SGLang streaming lifecycle race instead of capacity.
    # A deterministic early discriminator keeps each trial content-free while
    # forcing the near-limit body through prefill again.
    body = f"capacity-repeat-{repeat:02d} " + ("probe " * (repeats - 1))
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


def _usage_checks(
    *, context_tokens: int, generation_tokens: int, prompt_tokens: int, completion_tokens: int
) -> dict[str, bool]:
    return {
        "usage_accounting_present": prompt_tokens > 0 and completion_tokens > 0,
        "generation_reserve_met": prompt_tokens + generation_tokens <= context_tokens,
        "observed_total_within_context": prompt_tokens + completion_tokens <= context_tokens,
        "prompt_near_limit": (
            int(context_tokens * _MINIMUM_PROMPT_FRACTION)
            <= prompt_tokens
            <= context_tokens - generation_tokens
        ),
    }


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
    completion = None
    endpoint_rejected = False
    with GpuSampler() as sampler:
        try:
            completion = stream_chat_completion(
                base_url,
                api_key=api_key,
                messages=_context_prompt(
                    context_tokens,
                    generation_tokens,
                    repeat=repeat,
                ),
                timeout_sec=timeout_sec,
                max_tokens=generation_tokens,
                ca_file=ca_file,
            )
        except EndpointError:
            endpoint_rejected = True
    if sampler.error is not None:
        raise sampler.error
    gpu = sample_summary(sampler.samples)
    required_headroom_mib = max(512.0, gpu["total_mib"] * 0.05)
    headroom_met = gpu["minimum_free_mib"] >= required_headroom_mib
    if endpoint_rejected:
        return {
            "repeat": repeat,
            "context_target_tokens": context_tokens,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "ttft_sec": 0.0,
            "end_to_end_sec": 0.0,
            "decode_tokens_per_sec_after_first_token": 0.0,
            "finish_reason": "rejected",
            "reasoning_field_present": False,
            "usage_accounting_present": False,
            "generation_reserve_met": False,
            "observed_total_within_context": False,
            "prompt_near_limit": False,
            "generated_envelope_met": False,
            "required_headroom_mib": round(required_headroom_mib, 3),
            "headroom_met": headroom_met,
            "gpu": {key: round(number, 3) for key, number in gpu.items()},
            "gpu_identity": expected_gpu_identity(),
            "failure_class": "endpoint_or_capacity_rejection",
            "raw_prompt_retained": False,
            "raw_response_retained": False,
        }
    if completion is None:
        raise EndpointError("capacity completion state is missing")
    value = completion.completion
    usage_checks = _usage_checks(
        context_tokens=context_tokens,
        generation_tokens=generation_tokens,
        prompt_tokens=value.prompt_tokens,
        completion_tokens=value.completion_tokens,
    )
    generated_envelope_met = value.completion_tokens >= 256
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
        **usage_checks,
        "generated_envelope_met": generated_envelope_met,
        "required_headroom_mib": round(required_headroom_mib, 3),
        "headroom_met": headroom_met,
        "gpu": {key: round(number, 3) for key, number in gpu.items()},
        "gpu_identity": expected_gpu_identity(),
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
    ca_file: Path,
) -> dict[str, Any]:
    profile_context = configured_profile_context_tokens()
    profile_memory = configured_profile_mem_fraction_static()
    try:
        observed_memory = Decimal(str(mem_fraction_static))
    except (InvalidOperation, ValueError) as exc:
        raise EndpointError("capacity memory fraction is invalid") from exc
    if candidates != (profile_context,):
        raise EndpointError("capacity candidate must equal the exact profile context")
    if not observed_memory.is_finite() or observed_memory != Decimal(profile_memory):
        raise EndpointError("capacity memory fraction must equal the exact profile value")
    verify_remote_profile_epoch(
        base_url,
        api_key=api_key,
        timeout_sec=min(timeout_sec, 10.0),
        ca_file=ca_file,
    )
    runtime_epoch = runtime_process_epoch(
        base_url,
        api_key=api_key,
        timeout_sec=min(timeout_sec, 10.0),
        ca_file=ca_file,
    )
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
            trial["usage_accounting_present"]
            and trial["generation_reserve_met"]
            and trial["observed_total_within_context"]
            and trial["prompt_near_limit"]
            and trial["generated_envelope_met"]
            and trial["headroom_met"]
            for trial in candidate_trials
        )
        if passed:
            admitted.append(candidate)
        else:
            break
    if (
        runtime_process_epoch(
            base_url,
            api_key=api_key,
            timeout_sec=min(timeout_sec, 10.0),
            ca_file=ca_file,
        )
        != runtime_epoch
    ):
        raise EndpointError("runtime restarted during the capacity trial")
    return {
        "schema": "friday.secondary-context-capacity-trial.v1",
        "status": "measured_not_yet_certified",
        **evidence_identity(),
        "runtime_process_start_time_seconds": runtime_epoch,
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
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key-file", required=True, type=Path)
    parser.add_argument("--ca-file", required=True, type=Path)
    parser.add_argument("--profile-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--candidates", required=True, type=_parse_candidates)
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
        write_new_json(args.output, report)
        print(json.dumps({key: value for key, value in report.items() if key != "trials"}, sort_keys=True))
        return 0 if report["largest_passing_trial_tokens"] else 2
    except (EndpointError, GpuTelemetryError) as exc:
        print(f"capacity trial failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
