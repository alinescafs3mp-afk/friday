"""Bounded identity and generation probe for the optional SGLang endpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from endpoint_common import (
    EXPECTED_MODEL,
    EndpointError,
    atomic_write_json,
    chat_completion,
    configure_expected_model,
    load_api_key,
    normalize_base_url,
    request_json,
    verify_remote_profile_epoch,
)


def _model_ids(body: dict[str, Any]) -> list[str]:
    rows = body.get("data")
    if not isinstance(rows, list) or len(rows) > 128:
        raise EndpointError("/v1/models has an invalid bounded collection")
    model_ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise EndpointError("/v1/models contains an invalid row")
        model_ids.append(row["id"])
    return model_ids


def run_probe(base_url: str, api_key: str, timeout_sec: float, ca_file: Path | None) -> dict[str, Any]:
    normalized = normalize_base_url(base_url)
    models, model_latency = request_json(
        "GET",
        f"{normalized}/models",
        api_key=api_key,
        payload=None,
        timeout_sec=timeout_sec,
        ca_file=ca_file,
    )
    ids = _model_ids(models)
    if ids.count(EXPECTED_MODEL) != 1:
        raise EndpointError("/v1/models does not contain exactly one expected alias")

    completion = chat_completion(
        normalized,
        api_key=api_key,
        messages=[
            {
                "role": "system",
                "content": "Отвечай только финальным каналом; не раскрывай внутренние рассуждения.",
            },
            {"role": "user", "content": "Ответь одним коротким русским предложением: узел готов?"},
        ],
        timeout_sec=timeout_sec,
        max_tokens=96,
        temperature=0.0,
        ca_file=ca_file,
    )
    if not any(
        "а" <= character.casefold() <= "я" or character.casefold() == "ё" for character in completion.content
    ):
        raise EndpointError("bounded Russian canary returned no Cyrillic final content")
    return {
        "schema": "friday.secondary-endpoint-probe.v1",
        "status": "passed",
        "observed_at": datetime.now(UTC).isoformat(),
        "served_model_alias": EXPECTED_MODEL,
        "models_latency_ms": round(model_latency * 1000, 3),
        "completion_latency_ms": round(completion.latency_sec * 1000, 3),
        "prompt_tokens": completion.prompt_tokens,
        "completion_tokens": completion.completion_tokens,
        "finish_reason": completion.finish_reason,
        "reasoning_field_present": completion.reasoning_present,
        "final_content_sha256": hashlib.sha256(completion.content.encode("utf-8")).hexdigest(),
        "raw_content_retained": False,
        "api_key_retained": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:30000/v1")
    parser.add_argument("--api-key-file", required=True, type=Path)
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--profile-manifest", required=True, type=Path)
    parser.add_argument("--timeout-sec", default=30.0, type=float)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        global EXPECTED_MODEL
        EXPECTED_MODEL = configure_expected_model(args.profile_manifest, args.ca_file)
        api_key = load_api_key(args.api_key_file)
        if normalize_base_url(args.base_url).startswith("https://"):
            if args.ca_file is None:
                raise EndpointError("HTTPS probe requires an explicit CA file")
            verify_remote_profile_epoch(
                args.base_url,
                api_key=api_key,
                timeout_sec=args.timeout_sec,
                ca_file=args.ca_file,
            )
        report = run_probe(args.base_url, api_key, args.timeout_sec, args.ca_file)
        if args.output:
            atomic_write_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except EndpointError as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
