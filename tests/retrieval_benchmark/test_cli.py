from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import friday.retrieval_benchmark.cli as cli_module
import friday.retrieval_benchmark.io as io_module
from friday.retrieval_benchmark._canonical import RecallContractError
from friday.retrieval_benchmark.cli import (
    EXIT_HARNESS,
    EXIT_INPUT,
    EXIT_OK,
    EXIT_REGRESSION,
    main,
)
from friday.retrieval_benchmark.contracts import RecallCaseV1, RecallReportV1
from friday.retrieval_benchmark.harness import EphemeralRecallRunV1, cases_jsonl, observations_jsonl
from friday.retrieval_benchmark.io import (
    MAX_OUTPUT_ITEMS,
    parse_cases_jsonl,
    parse_observations_jsonl,
    read_cases,
    read_report,
    write_new,
    write_new_many,
)
from friday.retrieval_benchmark.metrics import score_recall
from tests.retrieval_benchmark.conftest import candidate_for, observation_for


def _inputs(tmp_path: Path, case: RecallCaseV1, *, hit: bool) -> tuple[Path, Path, RecallReportV1]:
    observation = observation_for(
        case,
        candidates=((candidate_for(case),) if hit else ()),
        complete=hit,
    )
    cases_path = tmp_path / "cases.jsonl"
    observations_path = tmp_path / "observations.jsonl"
    cases_path.write_text(cases_jsonl((case,)), encoding="ascii")
    cases_path.chmod(0o600)
    observations_path.write_text(observations_jsonl((observation,)), encoding="ascii")
    return cases_path, observations_path, score_recall((case,), (observation,))


def test_jsonl_parsers_round_trip(recall_case: RecallCaseV1) -> None:
    observation = observation_for(recall_case, complete=False)
    assert parse_cases_jsonl(cases_jsonl((recall_case,)).encode("ascii")) == (recall_case,)
    assert parse_observations_jsonl(observations_jsonl((observation,)).encode("ascii")) == (observation,)


def test_jsonl_rejects_duplicate_records(recall_case: RecallCaseV1) -> None:
    record = cases_jsonl((recall_case,)).encode("ascii")
    with pytest.raises(ValueError):
        parse_cases_jsonl(record + record)


def test_validate_cases_emits_body_free_canonical_summary(
    tmp_path: Path,
    recall_case: RecallCaseV1,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases_path, _observations_path, _report = _inputs(tmp_path, recall_case, hit=True)
    assert main(("validate", "cases", str(cases_path))) == EXIT_OK
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["kind"] == "cases"
    assert payload["count"] == 1
    assert recall_case.request.query not in output
    assert output.strip() == json.dumps(payload, sort_keys=True, separators=(",", ":"))


def test_score_emits_parseable_report(
    tmp_path: Path,
    recall_case: RecallCaseV1,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases_path, observations_path, expected = _inputs(tmp_path, recall_case, hit=True)
    assert main(("score", str(cases_path), str(observations_path))) == EXIT_OK
    report = RecallReportV1.parse(capsys.readouterr().out.rstrip("\n"))
    assert report == expected


def test_compare_equal_reports_does_not_claim_release_threshold(
    tmp_path: Path,
    recall_case: RecallCaseV1,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cases_path, _observations_path, report = _inputs(tmp_path, recall_case, hit=True)
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(f"{report.to_json()}\n", encoding="ascii")
    candidate.write_text(report.to_json(), encoding="ascii")
    assert main(("compare", str(baseline), str(candidate))) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["regression"] is False
    assert payload["release_threshold"] == "not_assessed"


def test_compare_returns_meaningful_regression_exit(
    tmp_path: Path,
    recall_case: RecallCaseV1,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cases_path, _observations_path, baseline_report = _inputs(tmp_path, recall_case, hit=True)
    worse_observation = observation_for(recall_case, complete=False)
    worse_report = score_recall((recall_case,), (worse_observation,))
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(baseline_report.to_json(), encoding="ascii")
    candidate.write_text(worse_report.to_json(), encoding="ascii")
    assert main(("compare", str(baseline), str(candidate))) == EXIT_REGRESSION
    assert json.loads(capsys.readouterr().out)["regression"] is True


def test_invalid_input_error_is_body_free(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "PRIVATE-PATH-AND-BODY-SENTINEL"
    path = tmp_path / sentinel
    path.write_text('{"query":"private body"}\n', encoding="ascii")
    assert main(("validate", "cases", str(path))) == EXIT_INPUT
    captured = capsys.readouterr()
    assert captured.out == ""
    assert sentinel not in captured.err
    assert "private body" not in captured.err
    assert json.loads(captured.err)["error"] == "input_contract_rejected"


def test_invalid_cli_shape_is_body_free(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "PRIVATE-QUERY-SENTINEL"
    assert main((sentinel,)) == EXIT_INPUT
    captured = capsys.readouterr()
    assert captured.out == ""
    assert sentinel not in captured.err
    assert "usage:" not in captured.err
    assert json.loads(captured.err)["error"] == "input_contract_rejected"

    assert main(("run-ephemeral", sentinel)) == EXIT_INPUT
    captured = capsys.readouterr()
    assert captured.out == ""
    assert sentinel not in captured.err
    assert "usage:" not in captured.err
    assert json.loads(captured.err)["error"] == "input_contract_rejected"


def test_run_ephemeral_dispatch_writes_explicit_new_sidecars(
    tmp_path: Path,
    recall_case: RecallCaseV1,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    report = score_recall((recall_case,), (observation,))
    run = EphemeralRecallRunV1((recall_case,), (observation,), report)
    monkeypatch.setattr(cli_module, "run_ephemeral", lambda: run)
    cases_out = tmp_path / "cases-out.jsonl"
    observations_out = tmp_path / "observations-out.jsonl"
    assert (
        main(
            (
                "run-ephemeral",
                "--cases-out",
                str(cases_out),
                "--observations-out",
                str(observations_out),
            )
        )
        == EXIT_OK
    )
    assert parse_cases_jsonl(cases_out.read_bytes()) == (recall_case,)
    assert parse_observations_jsonl(observations_out.read_bytes()) == (observation,)
    assert stat.S_IMODE(cases_out.stat().st_mode) == 0o600
    assert stat.S_IMODE(observations_out.stat().st_mode) == 0o600
    assert cases_out.stat().st_nlink == 1
    assert observations_out.stat().st_nlink == 1
    assert RecallReportV1.parse(capsys.readouterr().out.rstrip("\n")) == report


def test_sidecar_refuses_overwrite(
    tmp_path: Path,
    recall_case: RecallCaseV1,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observation = observation_for(recall_case, complete=False)
    report = score_recall((recall_case,), (observation,))
    monkeypatch.setattr(
        cli_module,
        "run_ephemeral",
        lambda: EphemeralRecallRunV1((recall_case,), (observation,), report),
    )
    existing = tmp_path / "existing.jsonl"
    existing.write_text("owner material", encoding="utf-8")
    assert main(("run-ephemeral", "--observations-out", str(existing))) == EXIT_INPUT
    assert existing.read_text(encoding="utf-8") == "owner material"
    assert json.loads(capsys.readouterr().err)["error"] == "input_contract_rejected"


def test_run_ephemeral_sidecars_fail_closed_as_one_group(
    tmp_path: Path,
    recall_case: RecallCaseV1,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observation = observation_for(recall_case, complete=False)
    report = score_recall((recall_case,), (observation,))
    monkeypatch.setattr(
        cli_module,
        "run_ephemeral",
        lambda: EphemeralRecallRunV1((recall_case,), (observation,), report),
    )
    cases_out = tmp_path / "new-cases.jsonl"
    observations_out = tmp_path / "existing-observations.jsonl"
    observations_out.write_bytes(b"owner material")

    assert (
        main(
            (
                "run-ephemeral",
                "--cases-out",
                str(cases_out),
                "--observations-out",
                str(observations_out),
            )
        )
        == EXIT_INPUT
    )
    assert not cases_out.exists()
    assert observations_out.read_bytes() == b"owner material"
    assert sorted(path.name for path in tmp_path.iterdir()) == [observations_out.name]
    assert json.loads(capsys.readouterr().err)["error"] == "input_contract_rejected"


def test_read_report_accepts_exact_cli_newline(
    tmp_path: Path,
    recall_case: RecallCaseV1,
) -> None:
    report = score_recall((recall_case,), (observation_for(recall_case, complete=False),))
    path = tmp_path / "report.json"
    path.write_text(f"{report.to_json()}\n", encoding="ascii")
    assert read_report(path) == report


def test_private_case_input_requires_current_owner_and_private_mode(
    tmp_path: Path,
    recall_case: RecallCaseV1,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(cases_jsonl((recall_case,)), encoding="ascii")
    path.chmod(0o644)
    with pytest.raises(RecallContractError):
        read_cases(path)

    path.chmod(0o600)
    assert read_cases(path) == (recall_case,)
    current_owner = os.geteuid()
    monkeypatch.setattr(io_module.os, "geteuid", lambda: current_owner + 1)
    with pytest.raises(RecallContractError):
        read_cases(path)


def test_inputs_reject_symlinks_hardlinks_and_non_regular_files(
    tmp_path: Path,
    recall_case: RecallCaseV1,
) -> None:
    original = tmp_path / "cases.jsonl"
    original.write_text(cases_jsonl((recall_case,)), encoding="ascii")
    original.chmod(0o600)
    symlink = tmp_path / "cases-link.jsonl"
    symlink.symlink_to(original)
    with pytest.raises(RecallContractError):
        read_cases(symlink)

    hardlink = tmp_path / "cases-hardlink.jsonl"
    os.link(original, hardlink)
    with pytest.raises(RecallContractError):
        read_cases(original)

    fifo = tmp_path / "cases.fifo"
    os.mkfifo(fifo, mode=0o600)
    with pytest.raises(RecallContractError):
        read_cases(fifo)
    with pytest.raises(RecallContractError):
        read_cases(tmp_path)


def test_read_rejects_file_that_changes_during_descriptor_bound_read(
    tmp_path: Path,
    recall_case: RecallCaseV1,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(cases_jsonl((recall_case,)), encoding="ascii")
    path.chmod(0o600)
    stable_identity = io_module._stable_identity
    calls = 0

    def changed_identity(status: os.stat_result) -> tuple[int, ...]:
        nonlocal calls
        identity = stable_identity(status)
        calls += 1
        if calls == 2:
            return (*identity[:-1], identity[-1] + 1)
        return identity

    monkeypatch.setattr(io_module, "_stable_identity", changed_identity)
    with pytest.raises(RecallContractError):
        read_cases(path)


def test_read_rejects_preopen_path_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intended = tmp_path / "intended.json"
    alternate = tmp_path / "alternate.json"
    backup = tmp_path / "backup.json"
    intended.write_bytes(b"expected")
    alternate.write_bytes(b"attacker")
    real_open = io_module.os.open

    def substitute_before_open(
        path: os.PathLike[str] | str,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        if Path(path) != intended:
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        intended.rename(backup)
        alternate.rename(intended)
        try:
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        finally:
            intended.rename(alternate)
            backup.rename(intended)

    monkeypatch.setattr(io_module.os, "open", substitute_before_open)
    with pytest.raises(RecallContractError):
        io_module.read_bounded(intended, maximum_bytes=100)
    assert intended.read_bytes() == b"expected"
    assert alternate.read_bytes() == b"attacker"


def test_write_new_refuses_symlink_and_removes_failed_atomic_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = tmp_path / "owner.txt"
    owner.write_bytes(b"owner material")
    output = tmp_path / "output.jsonl"
    output.symlink_to(owner)
    with pytest.raises(RecallContractError):
        write_new(output, b"replacement\n")
    assert owner.read_bytes() == b"owner material"

    output.unlink()

    def fail_link(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic publish failure")

    monkeypatch.setattr(io_module.os, "link", fail_link)
    with pytest.raises(RecallContractError):
        write_new(output, b"private sidecar\n")
    assert not output.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["owner.txt"]


def test_write_new_retains_single_private_sidecar_contract(tmp_path: Path) -> None:
    output = tmp_path / "single.jsonl"
    write_new(output, b"private sidecar\n")
    assert output.read_bytes() == b"private sidecar\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert output.stat().st_nlink == 1


def test_write_new_many_rolls_back_first_sidecar_when_second_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    real_link = io_module.os.link
    calls = 0

    def fail_second_link(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic second publish failure")
        real_link(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(io_module.os, "link", fail_second_link)
    with pytest.raises(RecallContractError):
        write_new_many(((first, b"first\n"), (second, b"second\n")))

    assert not first.exists()
    assert not second.exists()
    assert list(tmp_path.iterdir()) == []


def test_write_new_many_rejects_unsafe_parent_duplicates_and_excess_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o777)
    with pytest.raises(RecallContractError):
        write_new_many(((unsafe / "first", b"first"), (unsafe / "second", b"second")))
    assert list(unsafe.iterdir()) == []

    duplicate = tmp_path / "duplicate"
    with pytest.raises(RecallContractError):
        write_new_many(((duplicate, b"first"), (duplicate, b"second")))
    assert not duplicate.exists()

    too_many = tuple((tmp_path / f"bounded-{index}", b"value") for index in range(MAX_OUTPUT_ITEMS + 1))
    with pytest.raises(RecallContractError):
        write_new_many(too_many)
    assert not any(path.exists() for path, _value in too_many)

    monkeypatch.setattr(io_module, "MAX_OUTPUT_ITEM_BYTES", 4)
    with pytest.raises(RecallContractError):
        write_new_many(((tmp_path / "oversized", b"12345"),))
    assert not (tmp_path / "oversized").exists()


def test_write_new_many_detects_parent_inode_substitution_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "safe-parent"
    parent.mkdir(mode=0o700)
    moved = tmp_path / "moved-parent"
    real_link = io_module.os.link
    calls = 0

    def substitute_parent_after_first_link(*args: object, **kwargs: object) -> None:
        nonlocal calls
        real_link(*args, **kwargs)  # type: ignore[arg-type]
        calls += 1
        if calls == 1:
            parent.rename(moved)
            parent.mkdir(mode=0o700)

    monkeypatch.setattr(io_module.os, "link", substitute_parent_after_first_link)
    with pytest.raises(RecallContractError):
        write_new_many(
            (
                (parent / "first.jsonl", b"first\n"),
                (parent / "second.jsonl", b"second\n"),
            )
        )

    assert list(parent.iterdir()) == []
    assert list(moved.iterdir()) == []


def test_cli_distinguishes_ephemeral_os_failure_from_input_os_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_ephemeral() -> EphemeralRecallRunV1:
        raise OSError("PRIVATE-HARNESS-PATH")

    monkeypatch.setattr(cli_module, "run_ephemeral", fail_ephemeral)
    assert main(("run-ephemeral",)) == EXIT_HARNESS
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "PRIVATE-HARNESS-PATH" not in captured.err
    assert json.loads(captured.err)["error"] == "ephemeral_archive_path_failed"

    assert main(("validate", "cases", str(tmp_path / "missing.jsonl"))) == EXIT_INPUT
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"] == "input_contract_rejected"
