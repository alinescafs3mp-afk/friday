"""Command-line interface for privacy-safe retrieval recall evaluation."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final, NoReturn

from friday.retrieval_benchmark._canonical import RecallContractError, canonical_json
from friday.retrieval_benchmark.contracts import (
    RecallReportV1,
    case_manifest_sha256,
    observation_manifest_sha256,
)
from friday.retrieval_benchmark.conversation_harness import run_conversation_ephemeral
from friday.retrieval_benchmark.harness import (
    RecallHarnessError,
    cases_jsonl,
    observations_jsonl,
    run_ephemeral,
)
from friday.retrieval_benchmark.io import (
    read_cases,
    read_observations,
    read_report,
    write_new_many,
)
from friday.retrieval_benchmark.metrics import compare_reports, score_recall
from friday.retrieval_benchmark.parity import (
    ParityHarnessError,
    ParityReportV1,
    run_parity_ephemeral,
)

EXIT_OK: Final = 0
EXIT_INPUT: Final = 2
EXIT_HARNESS: Final = 3
EXIT_REGRESSION: Final = 4


class _ClosedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise RecallContractError("command-line contract rejected")


def _emit(
    payload: dict[str, object] | ParityReportV1 | RecallReportV1,
    *,
    error: bool = False,
) -> None:
    text = (
        payload.to_json()
        if isinstance(payload, (ParityReportV1, RecallReportV1))
        else canonical_json(payload)
    )
    stream = sys.stderr if error else sys.stdout
    stream.write(f"{text}\n")


def _validate(args: argparse.Namespace) -> int:
    path = Path(args.input)
    if args.kind == "cases":
        values = read_cases(path)
        payload: dict[str, object] = {
            "count": len(values),
            "kind": "cases",
            "manifest_sha256": case_manifest_sha256(values),
            "schema": "friday.retrieval-recall-validation.body-free.v1",
        }
    elif args.kind == "observations":
        observations = read_observations(path)
        payload = {
            "count": len(observations),
            "kind": "observations",
            "manifest_sha256": observation_manifest_sha256(observations),
            "schema": "friday.retrieval-recall-validation.body-free.v1",
        }
    else:
        report = read_report(path)
        payload = {
            "count": report.case_count,
            "kind": "report",
            "manifest_sha256": report.case_manifest_sha256,
            "report_sha256": report.report_sha256,
            "schema": "friday.retrieval-recall-validation.body-free.v1",
        }
    _emit(payload)
    return EXIT_OK


def _run_ephemeral(args: argparse.Namespace) -> int:
    try:
        result = run_ephemeral()
    except RecallHarnessError:
        raise
    except Exception as exc:
        raise RecallHarnessError("ephemeral archive path failed") from exc
    sidecars: list[tuple[Path, bytes]] = []
    if args.cases_out is not None:
        sidecars.append((Path(args.cases_out), cases_jsonl(result.cases).encode("ascii")))
    if args.observations_out is not None:
        sidecars.append(
            (
                Path(args.observations_out),
                observations_jsonl(result.observations).encode("ascii"),
            )
        )
    if sidecars:
        write_new_many(sidecars)
    _emit(result.report)
    return EXIT_OK


def _run_parity_ephemeral(_args: argparse.Namespace) -> int:
    _emit(run_parity_ephemeral())
    return EXIT_OK


def _run_conversation_ephemeral(_args: argparse.Namespace) -> int:
    try:
        result = run_conversation_ephemeral()
    except RecallHarnessError:
        raise
    except Exception as exc:
        raise RecallHarnessError("conversation archive path failed") from exc
    _emit(result.report)
    return EXIT_REGRESSION if result.gap_count else EXIT_OK


def _score(args: argparse.Namespace) -> int:
    report = score_recall(
        read_cases(Path(args.cases)),
        read_observations(Path(args.observations)),
    )
    _emit(report)
    return EXIT_OK


def _compare(args: argparse.Namespace) -> int:
    comparison = compare_reports(
        read_report(Path(args.baseline)),
        read_report(Path(args.candidate)),
    )
    _emit(comparison)
    return EXIT_REGRESSION if comparison["regression"] is True else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = _ClosedArgumentParser(
        prog="python -m friday.retrieval_benchmark",
        description="Offline privacy-safe archive-search recall benchmark",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate one canonical input")
    validate.add_argument("kind", choices=("cases", "observations", "report"))
    validate.add_argument("input")
    validate.set_defaults(handler=_validate)

    ephemeral = commands.add_parser("run-ephemeral", help="run the code-owned synthetic corpus")
    ephemeral.add_argument("--cases-out")
    ephemeral.add_argument("--observations-out")
    ephemeral.set_defaults(handler=_run_ephemeral)

    parity = commands.add_parser(
        "run-parity-ephemeral",
        help="run the separate body-free archive/legacy parity matrix",
    )
    parity.set_defaults(handler=_run_parity_ephemeral)

    conversation = commands.add_parser(
        "run-conversation-ephemeral",
        help="run the closed body-free conversation recall journey matrix",
    )
    conversation.set_defaults(handler=_run_conversation_ephemeral)

    score = commands.add_parser("score", help="score canonical cases and body-free observations")
    score.add_argument("cases")
    score.add_argument("observations")
    score.set_defaults(handler=_score)

    compare = commands.add_parser("compare", help="detect metric regressions")
    compare.add_argument("baseline")
    compare.add_argument("candidate")
    compare.set_defaults(handler=_compare)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        handler = args.handler
        return int(handler(args))
    except ParityHarnessError:
        _emit(
            {
                "error": "ephemeral_parity_path_failed",
                "schema": "friday.retrieval-recall-error.body-free.v1",
            },
            error=True,
        )
        return EXIT_HARNESS
    except RecallHarnessError:
        _emit(
            {
                "error": "ephemeral_archive_path_failed",
                "schema": "friday.retrieval-recall-error.body-free.v1",
            },
            error=True,
        )
        return EXIT_HARNESS
    except (RecallContractError, OSError, ValueError):
        _emit(
            {
                "error": "input_contract_rejected",
                "schema": "friday.retrieval-recall-error.body-free.v1",
            },
            error=True,
        )
        return EXIT_INPUT


__all__ = ["build_parser", "main"]
