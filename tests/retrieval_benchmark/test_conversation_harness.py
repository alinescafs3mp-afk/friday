from __future__ import annotations

import socket
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

import pytest

import friday.retrieval_benchmark.cli as cli_module
from friday.retrieval.archive_evidence_replay import (
    ArchiveEvidenceReplayStatus,
    unavailable_archive_evidence_replay_result,
)
from friday.retrieval.archive_search_contract import ArchiveMatchChannel, ArchiveSearchCorpus
from friday.retrieval_benchmark.cli import (
    EXIT_HARNESS,
    EXIT_INPUT,
    EXIT_OK,
    EXIT_REGRESSION,
    main,
)
from friday.retrieval_benchmark.contracts import RecallOutcomeV1
from friday.retrieval_benchmark.conversation_harness import (
    EphemeralConversationRecallRunV1,
    _exact_replay_model_sha256,
    run_conversation_ephemeral,
)
from friday.retrieval_benchmark.conversation_synthetic import conversation_synthetic_plan
from friday.retrieval_benchmark.harness import observations_jsonl
from friday.retrieval_benchmark.release import archive_search_release_sha256


@pytest.fixture(scope="module")
def conversation_run() -> EphemeralConversationRecallRunV1:
    return run_conversation_ephemeral()


def test_manifest_is_one_closed_six_by_four_conversation_matrix(
    conversation_run: EphemeralConversationRecallRunV1,
) -> None:
    assert len(conversation_run.cases) == 24
    assert Counter(item.matrix_cell for item in conversation_run.measurements) == {
        "archive": 4,
        "fallback": 4,
        "adjacent": 4,
        "diversity": 4,
        "replay": 4,
        "privacy": 4,
    }
    assert tuple(case.case_id for case in conversation_run.cases) == tuple(
        f"conversation.case.{ordinal:04d}" for ordinal in range(1, 25)
    )
    assert all(not case.expected_no_hit and len(case.alternatives) == 1 for case in conversation_run.cases)
    plan = conversation_synthetic_plan()
    assert len(plan.timestamp_resets) == 1
    timestamp_reset = plan.timestamp_resets[0]
    reset_row = next(row for row in plan.messages if row.message_id == timestamp_reset.message_id)
    assert timestamp_reset.initial_created_at != timestamp_reset.final_created_at
    assert reset_row.created_at == timestamp_reset.final_created_at
    assert reset_row.phase.value == "pre_backfill"


def test_real_path_recall_and_two_foreign_saturation_gaps_are_reproduced(
    conversation_run: EphemeralConversationRecallRunV1,
) -> None:
    assert conversation_run.writer_restart_resumed is True
    assert conversation_run.gap_count == 2
    assert {item.outcome for item in conversation_run.case_results} == {RecallOutcomeV1.HIT}
    assert {
        (
            item.matrix_cell,
            item.projection_contour,
            item.gap_codes,
            item.match_channels,
        )
        for item in conversation_run.measurements
        if item.gap_codes
    } == {
        (
            "fallback",
            "foreign_saturated",
            ("channel_mismatch",),
            (ArchiveMatchChannel.MESSAGE_HISTORY,),
        ),
        (
            "diversity",
            "foreign_saturated",
            ("channel_mismatch",),
            (ArchiveMatchChannel.MESSAGE_HISTORY,),
        ),
    }
    assert all(
        item.target_recalled
        and item.passage_window_exact
        and item.authorized_only
        and item.privacy_constraints_exact
        for item in conversation_run.measurements
    )
    recall_50 = dict(conversation_run.report.metrics)["candidate_recall_at_50"]
    recall_100 = dict(conversation_run.report.metrics)["candidate_recall_at_100"]
    assert (recall_50.numerator, recall_50.denominator, recall_50.value_ppm) == (
        23,
        24,
        958_333,
    )
    assert (recall_100.numerator, recall_100.denominator, recall_100.value_ppm) == (
        24,
        24,
        1_000_000,
    )
    assert replace(conversation_run, writer_restart_resumed=False).gap_count == 3


def test_fallback_and_foreign_saturation_remain_honest_history_only(
    conversation_run: EphemeralConversationRecallRunV1,
) -> None:
    history_only = [
        item
        for item in conversation_run.measurements
        if item.projection_contour
        in {"backfill_pending", "source_changed", "foreign_saturated", "accepted_boundary"}
    ]
    assert len(history_only) >= 7
    assert {item.match_channels for item in history_only} == {(ArchiveMatchChannel.MESSAGE_HISTORY,)}
    foreign_saturated = [
        item
        for item in conversation_run.measurements
        if item.projection_contour == "foreign_saturated"
    ]
    assert len(foreign_saturated) == 3
    assert sum(item.gap_codes == ("channel_mismatch",) for item in foreign_saturated) == 2
    assert all(item.target_recalled for item in foreign_saturated)


def test_adjacent_windows_keep_the_exact_matched_row_visible(
    conversation_run: EphemeralConversationRecallRunV1,
) -> None:
    adjacent = [item for item in conversation_run.measurements if item.matrix_cell == "adjacent"]
    assert len(adjacent) == 4
    assert all(item.matched_excerpt_visible and item.passage_window_exact for item in adjacent)
    plan = conversation_synthetic_plan()
    long_window = plan.diagnostic("conversation.case.0011")
    source_rows = [
        row for row in plan.messages if row.conversation_id == long_window.source_ref.canonical_object_id
    ]
    assert max(len(row.content) for row in source_rows) > 1_500


def test_privacy_cases_reject_same_principal_role_boundary_and_lifecycle_decoys(
    conversation_run: EphemeralConversationRecallRunV1,
) -> None:
    privacy = [item for item in conversation_run.measurements if item.matrix_cell == "privacy"]
    assert len(privacy) == 4
    assert all(item.authorized_only and item.privacy_constraints_exact for item in privacy)
    diagnostics = conversation_synthetic_plan().diagnostics[-4:]
    assert sum(bool(item.forbidden_message_ids) for item in diagnostics) == 2
    assert sum(bool(item.forbidden_source_refs) for item in diagnostics) == 1


def test_diversity_target_is_recalled_through_real_continuation(
    conversation_run: EphemeralConversationRecallRunV1,
) -> None:
    by_case = {item.case_id: item for item in conversation_run.measurements}
    continuation_case = conversation_synthetic_plan().diagnostic("conversation.case.0015")
    measurement = by_case[continuation_case.case.opaque_case_id]

    assert measurement.matrix_cell == "diversity"
    assert measurement.candidate_count == 25
    assert measurement.target_recalled is True


def test_four_selected_sources_replay_exactly_after_clean_restart(
    conversation_run: EphemeralConversationRecallRunV1,
) -> None:
    replay = [item for item in conversation_run.measurements if item.matrix_cell == "replay"]
    assert len(replay) == 4
    assert {item.replay_status for item in replay} == {ArchiveEvidenceReplayStatus.EXACT}
    assert all(item.replay_model_sha256 is not None for item in replay)
    assert sum(item.legacy_replay_compatible is True for item in replay) == 1
    assert all(
        item.replay_status is None
        and item.replay_model_sha256 is None
        and item.legacy_replay_compatible is None
        for item in conversation_run.measurements
        if item.matrix_cell != "replay"
    )


def test_closed_legacy_replay_becomes_a_measured_gap_without_reading_private_bytes() -> None:
    closed = unavailable_archive_evidence_replay_result(ArchiveSearchCorpus.MESSAGES)

    assert _exact_replay_model_sha256(closed) is None


def test_observations_report_and_measurements_are_body_free(
    conversation_run: EphemeralConversationRecallRunV1,
) -> None:
    serialized = (
        observations_jsonl(conversation_run.observations)
        + conversation_run.report.to_json()
        + repr(conversation_run.measurements)
    )
    forbidden = (
        "amaranth protocol",
        "Long adjacent context before",
        "synthetic accepted conversation recall request",
        "foreign0001",
        "recall-benchmark-principal",
        "recall-benchmark-foreign-principal",
        "conv_c000000000000001",
        "msg_d000000000000001",
        '"excerpt":',
        '"query":',
        "/home/",
        "/var/tmp/",
    )
    assert all(value not in serialized for value in forbidden)
    assert conversation_run.report.release_sha256 == archive_search_release_sha256()


def test_second_offline_run_is_byte_identical_and_never_uses_network(
    conversation_run: EphemeralConversationRecallRunV1,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("conversation benchmark attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    second = run_conversation_ephemeral()

    assert second.report.to_json() == conversation_run.report.to_json()
    assert observations_jsonl(second.observations) == observations_jsonl(conversation_run.observations)
    assert second.measurements == conversation_run.measurements


def test_conversation_cli_emits_only_the_existing_body_free_report(
    conversation_run: EphemeralConversationRecallRunV1,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli_module,
        "run_conversation_ephemeral",
        lambda: SimpleNamespace(report=conversation_run.report, gap_count=0),
    )

    assert main(("run-conversation-ephemeral",)) == EXIT_OK
    assert capsys.readouterr().out == f"{conversation_run.report.to_json()}\n"


def test_conversation_cli_uses_regression_exit_for_measured_gaps(
    conversation_run: EphemeralConversationRecallRunV1,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli_module,
        "run_conversation_ephemeral",
        lambda: SimpleNamespace(report=conversation_run.report, gap_count=1),
    )

    assert main(("run-conversation-ephemeral",)) == EXIT_REGRESSION
    assert capsys.readouterr().out == f"{conversation_run.report.to_json()}\n"


def test_conversation_cli_maps_unexpected_runner_failures_to_harness_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> None:
        raise ValueError("private raw failure")

    monkeypatch.setattr(cli_module, "run_conversation_ephemeral", fail)

    assert main(("run-conversation-ephemeral",)) == EXIT_HARNESS
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private raw failure" not in captured.err
    assert '"error":"ephemeral_archive_path_failed"' in captured.err


def test_conversation_cli_rejects_extra_private_arguments_body_free(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "PRIVATE-CONVERSATION-QUERY-SENTINEL"

    assert main(("run-conversation-ephemeral", sentinel)) == EXIT_INPUT
    captured = capsys.readouterr()
    assert captured.out == ""
    assert sentinel not in captured.err
