#!/usr/bin/env python3
"""Emit canonical, body-free synthetic P1 semantic-supervisor replay evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from friday.orchestration.supervisor_contracts import canonical_dumps  # noqa: E402
from friday.orchestration.supervisor_offline_evaluation import (  # noqa: E402
    OfflineEvaluationError,
    evaluate_offline_fixture_set,
)

DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "semantic_supervisor_offline_v1.json"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OfflineEvaluationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise OfflineEvaluationError(f"non-finite JSON number is forbidden: {value}")


def load_fixture_file(path: Path) -> object:
    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonfinite,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=("Replay synthetic fixtures offline; output is not live shadow or canary acceptance.")
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=DEFAULT_FIXTURES,
        help="closed synthetic fixture set (default: repository P1 fixture set)",
    )
    args = parser.parse_args(argv)
    try:
        report = evaluate_offline_fixture_set(load_fixture_file(args.fixtures))
    except (OSError, UnicodeError, json.JSONDecodeError, OfflineEvaluationError) as error:
        parser.error(str(error))
    sys.stdout.write(canonical_dumps(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
