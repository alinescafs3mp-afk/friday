"""The temporal gold set must stay hard, frozen, and privacy-safe.

The product already carries ``as_of`` and ``known_at`` end to end.  What was
missing was a measurement that can distinguish the right historical target from
the current one, including honest no-answer intervals.  These tests protect the
instrument before it is allowed to justify any ranking change.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import temporal_relational_bench as bench  # noqa: E402


def test_the_frozen_manifest_audits_cleanly() -> None:
    assert len(bench.GOLD_MANIFEST_SHA256) == 64
    assert bench.manifest_sha256() == bench.GOLD_MANIFEST_SHA256
    assert bench.audit_gold_set() == []


def test_candidate_manifest_file_pins_one_exact_candidate() -> None:
    manifest = bench._load_candidate_manifest()  # noqa: SLF001

    assert set(manifest) == {
        "version",
        "candidate_id",
        "base_commit",
        "candidate_path",
        "evaluator_path",
        "helper_path",
        "gold_manifest_sha256",
        "candidate_diff_sha256",
        "evaluator_blob_sha256",
        "helper_blob_sha256",
    }
    assert manifest["version"] == 2
    assert manifest["candidate_id"] == bench.CANDIDATE_ID
    assert manifest["base_commit"] == bench.CANDIDATE_BASE_COMMIT
    assert manifest["candidate_path"] == bench.CANDIDATE_PATH
    assert manifest["evaluator_path"] == bench.CANDIDATE_EVALUATOR_PATH
    assert manifest["helper_path"] == bench.CANDIDATE_HELPER_PATH
    assert manifest["gold_manifest_sha256"] == bench.GOLD_MANIFEST_SHA256
    assert bench._is_sha256(manifest["candidate_diff_sha256"])  # noqa: SLF001
    assert bench._is_sha256(manifest["evaluator_blob_sha256"])  # noqa: SLF001
    assert bench._is_sha256(manifest["helper_blob_sha256"])  # noqa: SLF001


def test_every_class_has_four_cases_in_each_whole_world_split() -> None:
    assert len(bench.GOLD_CASES) == 40
    counts = Counter((case.split, case.kind) for case in bench.GOLD_CASES)
    assert counts == Counter({(split, kind): 4 for split in bench.GOLD_SPLITS for kind in bench.GOLD_CLASSES})
    by_world: dict[str, list[bench.GoldCase]] = defaultdict(list)
    for case in bench.GOLD_CASES:
        by_world[case.world_id].append(case)
    assert len(by_world) == 20
    assert all(len(cases) == 2 for cases in by_world.values())
    assert all(len({case.split for case in cases}) == 1 for cases in by_world.values())


def test_target_pairs_are_symmetric_and_do_not_repeat_the_query_alias() -> None:
    worlds = {world.id: world for world in bench.WORLD_SPECS}
    for case in bench.GOLD_CASES:
        world = worlds[case.world_id]
        old = bench._document_text(world, "old")  # noqa: SLF001
        new = bench._document_text(world, "new")  # noqa: SLF001
        assert len(old) == len(new)
        assert world.alias.casefold() not in old.casefold()
        assert world.alias.casefold() not in new.casefold()
        assert world.alias.casefold() in case.query.casefold()


def test_explicit_time_routes_every_case_without_expanding_the_classifier() -> None:
    from friday.retrieval import is_relational_query

    for case in bench.GOLD_CASES:
        assert case.as_of or case.known_at_checkpoint
        if case.kind == "two_hop_chain":
            assert is_relational_query(case.query), "two-hop truth would be cut to depth one"
    # The natural one-hop formulation is intentionally outside the old measured
    # regex.  Its explicit temporal boundary, just like memory_search, enables the
    # graph without pretending S10b's missing human handoff exists.
    one_hop = next(case for case in bench.GOLD_CASES if case.kind == "valid_time_handover")
    assert is_relational_query(one_hop.query) is False
    assert bool(one_hop.as_of or one_hop.known_at_checkpoint) is True


def _candidate_manifest(
    exact_diff: bytes,
    *,
    evaluator_blob: bytes = b"frozen evaluator\n",
    helper_blob: bytes = b"frozen helper\n",
    **overrides: object,
) -> dict[str, object]:
    manifest: dict[str, object] = {
        "version": 2,
        "candidate_id": bench.CANDIDATE_ID,
        "base_commit": bench.CANDIDATE_BASE_COMMIT,
        "candidate_path": bench.CANDIDATE_PATH,
        "evaluator_path": bench.CANDIDATE_EVALUATOR_PATH,
        "helper_path": bench.CANDIDATE_HELPER_PATH,
        "gold_manifest_sha256": bench.GOLD_MANIFEST_SHA256,
        "candidate_diff_sha256": hashlib.sha256(exact_diff).hexdigest(),
        "evaluator_blob_sha256": hashlib.sha256(evaluator_blob).hexdigest(),
        "helper_blob_sha256": hashlib.sha256(helper_blob).hexdigest(),
    }
    manifest.update(overrides)
    return manifest


def _fake_candidate_git(
    *,
    exact_diff: bytes,
    evaluator_blob: bytes = b"frozen evaluator\n",
    helper_blob: bytes = b"frozen helper\n",
    failure: str = "",
    dirty: bytes = b"",
):
    def fake_git(*args: str) -> tuple[int, bytes]:
        if args[:2] == ("cat-file", "-e"):
            target = args[2]
            if failure == "manifest_not_in_head" and target.endswith(bench.CANDIDATE_MANIFEST_REPO_PATH):
                return 1, b""
            if failure == "candidate_not_in_head" and target.endswith(bench.CANDIDATE_PATH):
                return 1, b""
            if failure == "evaluator_not_in_head" and target.endswith(bench.CANDIDATE_EVALUATOR_PATH):
                return 1, b""
            if failure == "helper_not_in_head" and target.endswith(bench.CANDIDATE_HELPER_PATH):
                return 1, b""
            return 0, b""
        if args[:2] == ("status", "--porcelain=v1"):
            if failure == "status_unavailable":
                return 1, b""
            return 0, dirty
        if args[:2] == ("merge-base", "--is-ancestor"):
            return (1, b"") if failure == "base_not_ancestor" else (0, b"")
        if args and args[0] == "diff":
            assert args[1:-4] == bench._CANDIDATE_DIFF_OPTIONS  # noqa: SLF001
            assert args[-4:] == (bench.CANDIDATE_BASE_COMMIT, "HEAD", "--", bench.CANDIDATE_PATH)
            if failure == "diff_unavailable":
                return 1, b""
            if failure == "diff_mismatch":
                return 0, exact_diff + b"mutated"
            return 0, exact_diff
        if args and args[0] == "show":
            if args[1] == f"HEAD:{bench.CANDIDATE_EVALUATOR_PATH}":
                if failure == "evaluator_blob_unavailable":
                    return 1, b""
                if failure == "evaluator_digest_mismatch":
                    return 0, evaluator_blob + b"mutated"
                return 0, evaluator_blob
            if args[1] == f"HEAD:{bench.CANDIDATE_HELPER_PATH}":
                if failure == "helper_blob_unavailable":
                    return 1, b""
                if failure == "helper_digest_mismatch":
                    return 0, helper_blob + b"mutated"
                return 0, helper_blob
        raise AssertionError(f"unexpected git query: {args}")

    return fake_git


def test_exact_clean_committed_candidate_is_ready_without_running_holdout(monkeypatch) -> None:
    exact_diff = b"frozen exact retrieval diff\n"
    monkeypatch.setattr(bench, "_load_candidate_manifest", lambda: _candidate_manifest(exact_diff))
    monkeypatch.setattr(bench, "manifest_sha256", lambda: bench.GOLD_MANIFEST_SHA256)
    monkeypatch.setattr(bench, "_git", _fake_candidate_git(exact_diff=exact_diff))

    assert bench.candidate_manifest_complaints() == []


@pytest.mark.parametrize(
    ("overrides", "complaint"),
    (
        ({"candidate_id": "another_candidate"}, "candidate_id_mismatch"),
        ({"base_commit": "0" * 40}, "candidate_base_mismatch"),
        ({"candidate_path": "friday/retrieval/other.py"}, "candidate_path_mismatch"),
        ({"evaluator_path": "tools/other.py"}, "candidate_evaluator_path_mismatch"),
        ({"helper_path": "tools/other.py"}, "candidate_helper_path_mismatch"),
        ({"gold_manifest_sha256": "0" * 64}, "candidate_gold_digest_mismatch"),
        ({"candidate_diff_sha256": "not-a-sha256"}, "candidate_diff_digest_invalid"),
        ({"evaluator_blob_sha256": "not-a-sha256"}, "candidate_evaluator_digest_invalid"),
        ({"helper_blob_sha256": "not-a-sha256"}, "candidate_helper_digest_invalid"),
        ({"unexpected": True}, "candidate_manifest_fields_mismatch"),
    ),
)
def test_candidate_manifest_rejects_changed_frozen_fields(
    monkeypatch,
    overrides: dict[str, object],
    complaint: str,
) -> None:
    exact_diff = b"frozen exact retrieval diff\n"
    monkeypatch.setattr(
        bench,
        "_load_candidate_manifest",
        lambda: _candidate_manifest(exact_diff, **overrides),
    )
    monkeypatch.setattr(bench, "manifest_sha256", lambda: bench.GOLD_MANIFEST_SHA256)
    monkeypatch.setattr(bench, "_git", lambda *_args: (_ for _ in ()).throw(AssertionError("git reached")))

    assert complaint in bench.candidate_manifest_complaints()


@pytest.mark.parametrize(
    ("failure", "dirty", "complaint"),
    (
        ("manifest_not_in_head", b"", "candidate_manifest_not_in_head"),
        ("candidate_not_in_head", b"", "candidate_path_not_in_head"),
        ("evaluator_not_in_head", b"", "candidate_evaluator_not_in_head"),
        ("helper_not_in_head", b"", "candidate_helper_not_in_head"),
        ("status_unavailable", b"", "candidate_status_unavailable"),
        ("", b" M friday/retrieval/__init__.py\n", "candidate_paths_not_clean"),
        ("", b" M tools/temporal_relational_bench.py\n", "candidate_paths_not_clean"),
        ("", b" M tools/retrieval_bench.py\n", "candidate_paths_not_clean"),
        ("base_not_ancestor", b"", "candidate_base_not_ancestor"),
        ("evaluator_blob_unavailable", b"", "candidate_evaluator_blob_unavailable"),
        ("evaluator_digest_mismatch", b"", "candidate_evaluator_digest_mismatch"),
        ("helper_blob_unavailable", b"", "candidate_helper_blob_unavailable"),
        ("helper_digest_mismatch", b"", "candidate_helper_digest_mismatch"),
        ("diff_unavailable", b"", "candidate_diff_unavailable"),
        ("diff_mismatch", b"", "candidate_diff_digest_mismatch"),
    ),
)
def test_candidate_manifest_rejects_uncommitted_or_changed_git_state(
    monkeypatch,
    failure: str,
    dirty: bytes,
    complaint: str,
) -> None:
    exact_diff = b"frozen exact retrieval diff\n"
    monkeypatch.setattr(bench, "_load_candidate_manifest", lambda: _candidate_manifest(exact_diff))
    monkeypatch.setattr(bench, "manifest_sha256", lambda: bench.GOLD_MANIFEST_SHA256)
    monkeypatch.setattr(
        bench,
        "_git",
        _fake_candidate_git(exact_diff=exact_diff, failure=failure, dirty=dirty),
    )

    assert complaint in bench.candidate_manifest_complaints()


def test_holdout_stays_sealed_before_dispatch_regardless_of_checkout_timing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(bench, "audit_gold_set", lambda: [])
    monkeypatch.setattr(bench, "candidate_manifest_complaints", lambda: ["candidate_paths_not_clean"])
    monkeypatch.setattr(
        bench,
        "_run_paired_holdout",
        lambda: (_ for _ in ()).throw(AssertionError("holdout pair reached")),
    )

    assert bench._baseline_command("holdout") == 2  # noqa: SLF001
    assert "sealed" in capsys.readouterr().err


def test_ready_holdout_dispatches_without_running_the_real_pair(monkeypatch) -> None:
    dispatched: list[bool] = []
    monkeypatch.setattr(bench, "audit_gold_set", lambda: [])
    monkeypatch.setattr(bench, "candidate_manifest_complaints", lambda: [])
    monkeypatch.setattr(bench, "_verified_tool_root", lambda: bench.ROOT / "tools")
    monkeypatch.setattr(bench, "_run_paired_holdout", lambda: dispatched.append(True) or 17)

    assert bench._baseline_command("holdout") == 17  # noqa: SLF001
    assert dispatched == [True]


def test_public_entry_reexecs_the_committed_evaluator_before_any_calibration(monkeypatch) -> None:
    dispatched: list[str] = []
    monkeypatch.delenv(bench._COMMITTED_EVALUATOR_ENV, raising=False)  # noqa: SLF001
    monkeypatch.setattr(bench, "audit_gold_set", lambda: [])
    monkeypatch.setattr(bench, "candidate_manifest_complaints", lambda: [])
    monkeypatch.setattr(
        bench,
        "_verified_tool_root",
        lambda: (_ for _ in ()).throw(bench._ClosedArmError("committed_evaluator_binding_missing")),
    )
    monkeypatch.setattr(
        bench,
        "_run_through_committed_evaluator",
        lambda split: dispatched.append(split) or 19,
    )
    monkeypatch.setattr(
        bench,
        "_run_candidate_calibration",
        lambda: (_ for _ in ()).throw(AssertionError("live evaluator reached calibration")),
    )

    assert bench._baseline_command("calibration") == 19  # noqa: SLF001
    assert dispatched == ["calibration"]


def test_runtime_builder_proves_the_graph_truth_before_ranking(storage) -> None:
    graph, cases = bench.build_runtime_cases(
        storage,
        split="calibration",
        include_fillers=False,
    )
    assert len(cases) == 20
    assert bench._graph_truth_complaints(graph, cases) == []  # noqa: SLF001
    checkpoints = [case for case in cases if case.spec.known_at_checkpoint]
    assert checkpoints and all(case.known_at.endswith("Z") for case in checkpoints)
    assert all(case.known_at for case in checkpoints)


class _SpySearcher:
    _reranker = None

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def search(self, user_id: str, query: str, **kwargs):
        self.calls.append({"user_id": user_id, "query": query, **kwargs})
        return {
            "results": [{"id": "ko-expected"}, {"id": "ko-forbidden"}],
            "strategy": {"embeddings": False},
            "graph_context": {
                "expanded": True,
                "as_of": kwargs["as_of"],
                "known_at": kwargs["known_at"],
                "nodes": [{"id": "ent-expected"}],
            },
        }


class _RankScriptSearcher:
    _reranker = None

    def __init__(self, rankings: dict[str, list[str]]) -> None:
        self.rankings = rankings

    async def search(self, user_id: str, query: str, **kwargs):
        return {
            "results": [{"id": item} for item in self.rankings[query]],
            "strategy": {"embeddings": False},
            "graph_context": {
                "expanded": True,
                "as_of": kwargs["as_of"],
                "known_at": kwargs["known_at"],
                "nodes": [{"id": f"entity-{query}"}],
            },
        }


def _positive_runtime(case_id: str) -> bench.RuntimeCase:
    case = bench.GoldCase(
        id=case_id,
        world_id=case_id,
        split="calibration",
        kind="valid_time_handover",
        query=case_id,
        as_of="2024-01-01",
        known_at_checkpoint="",
        expected_knowledge_ids=(f"expected-{case_id}",),
        forbidden_knowledge_ids=(f"forbidden-{case_id}",),
        expected_entity_ids=(f"entity-{case_id}",),
        forbidden_entity_ids=(f"forbidden-entity-{case_id}",),
    )
    return bench.RuntimeCase(case, "")


@pytest.mark.asyncio
async def test_measurement_forwards_both_boundaries_and_never_writes_usage(storage) -> None:
    storage.ensure_user(bench._USER_ID)  # noqa: SLF001
    case = bench.GoldCase(
        id="synthetic-wiring",
        world_id="synthetic-wiring",
        split="calibration",
        kind="bitemporal_replacement",
        query="synthetic query",
        as_of="2024-01-01",
        known_at_checkpoint="initial",
        expected_knowledge_ids=("ko-expected",),
        forbidden_knowledge_ids=("ko-forbidden",),
        expected_entity_ids=("ent-expected",),
        forbidden_entity_ids=("ent-forbidden",),
    )
    runtime = bench.RuntimeCase(case, "2025-01-01T00:00:00.000000Z")
    searcher = _SpySearcher()

    report = await bench.measure_baseline(
        storage,
        object(),
        searcher,
        [runtime],
        embeddings_required=False,
    )

    assert report["correct"] == 1
    assert report["structure_unchanged"] is True
    assert searcher.calls[0]["as_of"] == case.as_of
    assert searcher.calls[0]["known_at"] == runtime.known_at
    assert searcher.calls[0]["graph_expansion"] is True
    assert searcher.calls[0]["record_usage"] is False


@pytest.mark.asyncio
async def test_mrr_counts_every_positive_case_and_scores_a_miss_as_zero(storage) -> None:
    storage.ensure_user(bench._USER_ID)  # noqa: SLF001
    runtime = [_positive_runtime(case_id) for case_id in ("rank-one", "rank-two", "miss")]
    searcher = _RankScriptSearcher(
        {
            "rank-one": ["expected-rank-one", "neutral-one"],
            "rank-two": ["neutral-two", "expected-rank-two"],
            "miss": ["neutral-miss"],
        }
    )

    report = await bench.measure_baseline(
        storage,
        object(),
        searcher,
        runtime,
        embeddings_required=False,
    )

    assert report["positive_cases"] == 3
    assert report["expected_hits_at_10"] == 2
    assert report["mrr"] == 0.5  # (1 + 1/2 + 0) / 3, not the conditional 0.75


@pytest.mark.asyncio
async def test_measurement_reports_the_runtime_split_instead_of_a_hardcoded_label(storage) -> None:
    storage.ensure_user(bench._USER_ID)  # noqa: SLF001
    runtime = _positive_runtime("split-probe")
    runtime = bench.RuntimeCase(
        runtime.spec.__class__(**{**runtime.spec.__dict__, "split": "synthetic-probe"}),
        runtime.known_at,
    )
    searcher = _RankScriptSearcher({"split-probe": ["expected-split-probe"]})

    report = await bench.measure_baseline(
        storage,
        object(),
        searcher,
        [runtime],
        embeddings_required=False,
    )

    assert report["split"] == "synthetic-probe"


def _accepted_calibration_report() -> dict:
    return {
        "cases": 20,
        "positive_cases": 16,
        "correct": 12,
        "expected_hits_at_10": 8,
        "no_answer_cases": 4,
        "no_answer_correct": 4,
        "forbidden_hits_at_10": 0,
        "by_class": {kind: {"positive_correct": 1, "positive_cases": 1} for kind in bench.GOLD_CLASSES},
        "graph_failures": 0,
        "rerank_applied_cases": 20,
        "reranker_calls": 20,
        "reranker_failures": 0,
        "snapshot_failures": 0,
        "embedding_failures": 0,
        "structure_unchanged": True,
    }


def test_calibration_acceptance_separates_quality_from_measurement_infrastructure() -> None:
    accepted = _accepted_calibration_report()
    assert bench.calibration_acceptance(accepted)["accepted"] is True

    quality_rejected = {**accepted, "expected_hits_at_10": 7}
    quality = bench.calibration_acceptance(quality_rejected)
    assert quality["accepted"] is False
    assert "minimum_expected_hits_at_10" in quality["failed_checks"]

    infrastructure_failed = {**accepted, "graph_failures": 1}
    assert bench.calibration_acceptance(infrastructure_failed)["accepted"] is True
    assert bench._infrastructure_failure_count(infrastructure_failed) == 1  # noqa: SLF001


@pytest.mark.parametrize(
    ("field", "value", "failed_check"),
    (
        ("positive_cases", 15, "frozen_case_counts"),
        ("correct", 11, "minimum_correct_at_10"),
        ("no_answer_correct", 3, "all_no_answer_cases_correct"),
        ("forbidden_hits_at_10", 1, "no_forbidden_hits_at_10"),
    ),
)
def test_each_scalar_calibration_guard_rejects_a_valid_but_bad_run(
    field: str,
    value: int,
    failed_check: str,
) -> None:
    report = {**_accepted_calibration_report(), field: value}
    acceptance = bench.calibration_acceptance(report)
    assert acceptance["accepted"] is False
    assert failed_check in acceptance["failed_checks"]


def test_calibration_requires_a_positive_hit_in_every_temporal_class() -> None:
    report = _accepted_calibration_report()
    report["by_class"] = {key: dict(value) for key, value in report["by_class"].items()}
    missing_class = bench.GOLD_CLASSES[0]
    report["by_class"][missing_class]["positive_correct"] = 0

    acceptance = bench.calibration_acceptance(report)

    assert acceptance["accepted"] is False
    assert acceptance["uncovered_positive_classes"] == [missing_class]


def test_forbidden_rank_is_part_of_correctness_not_advisory_metadata() -> None:
    positive = bench.GoldCase(
        id="positive",
        world_id="world",
        split="calibration",
        kind="valid_time_handover",
        query="query",
        as_of="2024-01-01",
        known_at_checkpoint="",
        expected_knowledge_ids=("expected",),
        forbidden_knowledge_ids=("forbidden",),
        expected_entity_ids=("expected-entity",),
        forbidden_entity_ids=("forbidden-entity",),
    )
    assert bench._case_outcome(positive, ["expected", "forbidden"])[0] is True  # noqa: SLF001
    assert bench._case_outcome(positive, ["forbidden", "expected"])[0] is False  # noqa: SLF001
    missing = positive.__class__(
        **{
            **positive.__dict__,
            "id": "no-answer",
            "expected_knowledge_ids": (),
        }
    )
    assert bench._case_outcome(missing, ["unrelated"])[0] is True  # noqa: SLF001
    assert bench._case_outcome(missing, ["forbidden"])[0] is False  # noqa: SLF001


def _synthetic_measurement_report(
    split: str,
    *,
    positive_hits: set[str],
    rerank_applied_ids: set[str],
) -> dict:
    specs = [case for case in bench.GOLD_CASES if case.split == split]
    rows: list[dict] = []
    for case in specs:
        positive = bool(case.expected_knowledge_ids)
        correct = case.id in positive_hits if positive else True
        rows.append(
            {
                "case": case.id,
                "class": case.kind,
                "positive": positive,
                "correct": correct,
                "expected_rank": 1 if positive and correct else None,
                "forbidden_ranks": [None for _ in case.forbidden_knowledge_ids],
                "expected_entity_present": positive,
                "forbidden_entity_present": False,
                "latency_ms": 1.0,
                "graph_failed": False,
                "rerank_applied": case.id in rerank_applied_ids,
                "reranker_failed": False,
                "snapshot_failed": False,
                "embedding_failed": False,
            }
        )
    positive_rows = [row for row in rows if row["positive"]]
    no_answer_rows = [row for row in rows if not row["positive"]]
    by_class = {
        kind: {
            "cases": sum(row["class"] == kind for row in rows),
            "correct": sum(row["class"] == kind and row["correct"] for row in rows),
            "positive_cases": sum(row["class"] == kind and row["positive"] for row in rows),
            "positive_correct": sum(
                row["class"] == kind and row["positive"] and row["correct"] for row in rows
            ),
        }
        for kind in bench.GOLD_CLASSES
    }
    return {
        "fixture_sha256": bench.GOLD_MANIFEST_SHA256,
        "split": split,
        "cases": len(rows),
        "correct": sum(row["correct"] for row in rows),
        "case_correct_at_10": round(sum(row["correct"] for row in rows) / len(rows), 4),
        "mrr": round(sum(row["expected_rank"] is not None for row in positive_rows) / 16, 4),
        "positive_cases": len(positive_rows),
        "expected_hits_at_10": sum(row["expected_rank"] is not None for row in positive_rows),
        "no_answer_cases": len(no_answer_rows),
        "no_answer_correct": sum(row["correct"] for row in no_answer_rows),
        "forbidden_hits_at_10": 0,
        "positive_expected_entity_present": len(positive_rows),
        "forbidden_entity_present": 0,
        "by_class": by_class,
        "p50_latency_ms": 1.0,
        "p95_latency_ms": 1.0,
        "graph_failures": 0,
        "rerank_applied_cases": len(rerank_applied_ids),
        "reranker_calls": len(rerank_applied_ids),
        "reranker_failures": 0,
        "snapshot_failures": 0,
        "embedding_failures": 0,
        "structure_unchanged": True,
        "per_case": rows,
    }


def _synthetic_control(split: str) -> dict:
    projection = [
        {"case": case.id, "result_ids": [f"ko-temporal-control-{case.id}"]}
        for case in bench.GOLD_CASES
        if case.split == split
    ]
    control = {
        "contract": "non_temporal_ranking_projection_v1",
        "fixture_sha256": bench.GOLD_MANIFEST_SHA256,
        "split": split,
        "cases": len(projection),
        "failures": 0,
        "reranker_calls": len(projection),
        "reranker_failures": 0,
        "structure_unchanged": True,
        "projection_sha256": "",
        "projection": projection,
    }
    control["projection_sha256"] = hashlib.sha256(
        bench._control_projection_bytes(control)  # noqa: SLF001
    ).hexdigest()
    return control


def _synthetic_arm(
    arm: str,
    split: str,
    *,
    positive_hits: set[str],
) -> dict:
    specs = [case for case in bench.GOLD_CASES if case.split == split]
    if arm == "exact_base":
        rerank_applied_ids = {case.id for case in specs}
        package = "base_archive"
    else:
        rerank_applied_ids = {case.id for case in specs if not case.expected_knowledge_ids}
        package = "base_archive_plus_head_retrieval"
    report = _synthetic_measurement_report(
        split,
        positive_hits=positive_hits,
        rerank_applied_ids=rerank_applied_ids,
    )
    return {
        "protocol": bench._ARM_PROTOCOL,  # noqa: SLF001
        "request_id": "0" * 64,
        "ok": True,
        "arm": arm,
        "split": split,
        "provenance": {
            "contract": "git_object_package_v1",
            "base_commit": bench.CANDIDATE_BASE_COMMIT,
            "package": package,
            "friday_modules_confined": True,
        },
        "runtime": {
            "embeddings_remote_enabled": True,
            "reranker_configured": True,
            "rerank_top": 40,
            "rerank_confident_min": 0.10,
            "embedding_index_complete": True,
            "embedding_object_vectors": 140,
            "embedding_chunk_vectors": 0,
            "record_usage": False,
        },
        "report": report,
        "control": _synthetic_control(split),
    }


def test_model_runtime_selection_never_forwards_live_paths_or_unrelated_secrets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_file = tmp_path / "operator.env"
    env_file.write_text(
        "\n".join(
            (
                "FRIDAY_LLM_API_KEY=synthetic-shared-key",
                "FRIDAY_EMBEDDINGS_ENABLED=1",
                "FRIDAY_EMBEDDINGS_BASE_URL=http://127.0.0.1:9001/v1",
                "FRIDAY_RERANK_BASE_URL=http://127.0.0.1:9002",
                "FRIDAY_RERANK_MODEL=synthetic-reranker",
                "FRIDAY_RERANK_TOP=40",
                "FRIDAY_DATABASE_PATH=/sentinel/live.sqlite3",
                "FRIDAY_API_TOKEN=must-not-cross",
                "JERICHO_HOME=/sentinel/live-home",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FRIDAY_ENV_FILE", str(env_file))

    selected = bench._model_runtime_settings()  # noqa: SLF001

    assert selected["FRIDAY_LLM_ENABLED"] == "0"
    assert selected["FRIDAY_EMBEDDINGS_API_KEY"] == "synthetic-shared-key"
    assert selected["FRIDAY_RERANK_API_KEY"] == "synthetic-shared-key"
    assert "FRIDAY_DATABASE_PATH" not in selected
    assert "FRIDAY_API_TOKEN" not in selected
    assert "JERICHO_HOME" not in selected


def test_scratch_confinement_rejects_any_data_path_escape(tmp_path: Path, monkeypatch) -> None:
    scratch = tmp_path / "scratch"
    for name, relative in bench._SCRATCH_ENV_PATHS.items():  # noqa: SLF001
        monkeypatch.setenv(name, str((scratch / relative).resolve()))
    monkeypatch.setenv("FRIDAY_ENV_FILE", str((scratch / "config" / "missing.env").resolve()))
    settings = SimpleNamespace(database_path=scratch / "data" / "state" / "friday.sqlite3")

    bench._assert_scratch_settings(settings, scratch)  # noqa: SLF001
    settings.database_path = tmp_path / "sentinel-live.sqlite3"

    with pytest.raises(bench._ClosedArmError, match="scratch_settings_escape"):
        bench._assert_scratch_settings(settings, scratch)  # noqa: SLF001


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return payload.getvalue()


def test_exact_base_extraction_uses_only_the_pinned_friday_git_object(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    archive = _tar_bytes(
        {
            "friday/__init__.py": b"BASE = True\n",
            "friday/retrieval/__init__.py": b"BASE_RETRIEVAL = True\n",
        }
    )

    def fake_git(*args: str):
        calls.append(args)
        return 0, archive

    monkeypatch.setattr(bench, "_git", fake_git)
    bench._extract_exact_base_friday(tmp_path)  # noqa: SLF001

    assert calls == [("archive", "--format=tar", bench.CANDIDATE_BASE_COMMIT, "--", "friday")]
    assert {item.name for item in tmp_path.iterdir()} == {"friday"}
    assert (tmp_path / "friday" / "retrieval" / "__init__.py").read_bytes() == b"BASE_RETRIEVAL = True\n"


def test_candidate_package_is_base_plus_one_committed_head_blob(tmp_path: Path, monkeypatch) -> None:
    def fake_extract(destination: Path) -> None:
        target = destination / "friday" / "retrieval"
        target.mkdir(parents=True)
        (destination / "friday" / "__init__.py").write_text("BASE = True\n", encoding="utf-8")
        (target / "__init__.py").write_text("BASE_RETRIEVAL = True\n", encoding="utf-8")

    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str):
        calls.append(args)
        return 0, b"CANDIDATE_RETRIEVAL = True\n"

    monkeypatch.setattr(bench, "_extract_exact_base_friday", fake_extract)
    monkeypatch.setattr(bench, "_git", fake_git)

    baseline_root, candidate_root = bench._prepare_paired_package_trees(tmp_path)  # noqa: SLF001

    assert calls == [("show", f"HEAD:{bench.CANDIDATE_PATH}")]
    assert (baseline_root / bench.CANDIDATE_PATH).read_text(encoding="utf-8") == "BASE_RETRIEVAL = True\n"
    assert (candidate_root / bench.CANDIDATE_PATH).read_text(encoding="utf-8") == (
        "CANDIDATE_RETRIEVAL = True\n"
    )
    assert (candidate_root / "friday" / "__init__.py").read_text(encoding="utf-8") == "BASE = True\n"


def test_committed_tool_root_contains_only_manifest_bound_head_blobs(tmp_path: Path, monkeypatch) -> None:
    evaluator = b"frozen evaluator\n"
    helper = b"frozen helper\n"
    manifest = _candidate_manifest(b"diff", evaluator_blob=evaluator, helper_blob=helper)
    monkeypatch.setattr(bench, "_load_candidate_manifest", lambda: manifest)

    def fake_head_blob(repo_path: str) -> bytes | None:
        return {
            bench.CANDIDATE_EVALUATOR_PATH: evaluator,
            bench.CANDIDATE_HELPER_PATH: helper,
        }.get(repo_path)

    monkeypatch.setattr(bench, "_head_blob", fake_head_blob)
    capability = "a" * 64
    tool_root = bench._materialize_committed_tools(tmp_path, capability=capability)  # noqa: SLF001

    assert {item.name for item in tmp_path.iterdir()} == {"tools", bench._TOOL_CAPABILITY_NAME}  # noqa: SLF001
    assert {item.name for item in tool_root.iterdir()} == {
        Path(bench.CANDIDATE_EVALUATOR_PATH).name,
        Path(bench.CANDIDATE_HELPER_PATH).name,
    }
    assert (tool_root / Path(bench.CANDIDATE_EVALUATOR_PATH).name).read_bytes() == evaluator
    assert (tool_root / Path(bench.CANDIDATE_HELPER_PATH).name).read_bytes() == helper
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert tool_root.stat().st_mode & 0o777 == 0o700
    assert all(item.stat().st_mode & 0o777 == 0o600 for item in tool_root.iterdir())


def _private_tool_projection(
    tmp_path: Path,
    *,
    evaluator: bytes,
    helper: bytes,
    capability: str,
) -> tuple[Path, Path, Path]:
    parent = tmp_path / "friday-temporal-committed-tools-test"
    parent.mkdir(mode=0o700)
    tool_root = parent / "tools"
    tool_root.mkdir(mode=0o700)
    evaluator_path = tool_root / Path(bench.CANDIDATE_EVALUATOR_PATH).name
    helper_path = tool_root / Path(bench.CANDIDATE_HELPER_PATH).name
    evaluator_path.write_bytes(evaluator)
    helper_path.write_bytes(helper)
    evaluator_path.chmod(0o600)
    helper_path.chmod(0o600)
    capability_path = parent / bench._TOOL_CAPABILITY_NAME  # noqa: SLF001
    capability_path.write_text(hashlib.sha256(capability.encode()).hexdigest(), encoding="ascii")
    capability_path.chmod(0o600)
    return tool_root, evaluator_path, helper_path


def test_verified_evaluator_boundary_rejects_a_mutated_materialized_helper(
    tmp_path: Path,
    monkeypatch,
) -> None:
    evaluator = b"frozen evaluator\n"
    helper = b"frozen helper\n"
    capability = "b" * 64
    manifest = _candidate_manifest(b"diff", evaluator_blob=evaluator, helper_blob=helper)
    tool_root, evaluator_path, helper_path = _private_tool_projection(
        tmp_path,
        evaluator=evaluator,
        helper=helper,
        capability=capability,
    )
    monkeypatch.setattr(bench, "_load_candidate_manifest", lambda: manifest)
    monkeypatch.setattr(bench, "__file__", str(evaluator_path))
    monkeypatch.setenv(bench._VERIFIED_TOOL_ROOT_ENV, str(tool_root))  # noqa: SLF001
    monkeypatch.setenv(bench._VERIFIED_TOOL_CAPABILITY_ENV, capability)  # noqa: SLF001
    monkeypatch.setenv(  # noqa: SLF001
        bench._COMMITTED_EVALUATOR_ENV,
        str(manifest["evaluator_blob_sha256"]),
    )

    assert bench._verified_tool_root() == tool_root  # noqa: SLF001
    helper_path.write_bytes(helper + b"mutated")
    with pytest.raises(bench._ClosedArmError, match="committed_helper_digest_mismatch"):
        bench._verified_tool_root()  # noqa: SLF001


def test_live_repository_tools_can_never_claim_the_verified_boundary(monkeypatch) -> None:
    manifest = bench._load_candidate_manifest()  # noqa: SLF001
    monkeypatch.setenv(bench._VERIFIED_TOOL_ROOT_ENV, str(bench.ROOT / "tools"))  # noqa: SLF001
    monkeypatch.setenv(bench._VERIFIED_TOOL_CAPABILITY_ENV, "c" * 64)  # noqa: SLF001
    monkeypatch.setenv(  # noqa: SLF001
        bench._COMMITTED_EVALUATOR_ENV,
        str(manifest["evaluator_blob_sha256"]),
    )

    with pytest.raises(bench._ClosedArmError, match="committed_tool_root_untrusted"):
        bench._verified_tool_root()  # noqa: SLF001


@pytest.mark.parametrize("shadow_name", ("json.py", "friday.py"))
def test_any_extra_or_shadow_module_closes_the_private_tool_projection(
    tmp_path: Path,
    monkeypatch,
    shadow_name: str,
) -> None:
    evaluator = b"frozen evaluator\n"
    helper = b"frozen helper\n"
    capability = "d" * 64
    manifest = _candidate_manifest(b"diff", evaluator_blob=evaluator, helper_blob=helper)
    tool_root, evaluator_path, _helper_path = _private_tool_projection(
        tmp_path,
        evaluator=evaluator,
        helper=helper,
        capability=capability,
    )
    shadow = tool_root / shadow_name
    shadow.write_text("raise RuntimeError('shadow')\n", encoding="utf-8")
    shadow.chmod(0o600)
    monkeypatch.setattr(bench, "_load_candidate_manifest", lambda: manifest)
    monkeypatch.setattr(bench, "__file__", str(evaluator_path))
    monkeypatch.setenv(bench._VERIFIED_TOOL_ROOT_ENV, str(tool_root))  # noqa: SLF001
    monkeypatch.setenv(bench._VERIFIED_TOOL_CAPABILITY_ENV, capability)  # noqa: SLF001
    monkeypatch.setenv(  # noqa: SLF001
        bench._COMMITTED_EVALUATOR_ENV,
        str(manifest["evaluator_blob_sha256"]),
    )

    with pytest.raises(bench._ClosedArmError, match="committed_tool_root_shape_invalid"):
        bench._verified_tool_root()  # noqa: SLF001


def test_symlinked_expected_tool_blob_closes_the_private_projection(tmp_path: Path, monkeypatch) -> None:
    evaluator = b"frozen evaluator\n"
    helper = b"frozen helper\n"
    capability = "e" * 64
    manifest = _candidate_manifest(b"diff", evaluator_blob=evaluator, helper_blob=helper)
    tool_root, evaluator_path, helper_path = _private_tool_projection(
        tmp_path,
        evaluator=evaluator,
        helper=helper,
        capability=capability,
    )
    outside = tmp_path / "outside-helper.py"
    outside.write_bytes(helper)
    helper_path.unlink()
    helper_path.symlink_to(outside)
    monkeypatch.setattr(bench, "_load_candidate_manifest", lambda: manifest)
    monkeypatch.setattr(bench, "__file__", str(evaluator_path))
    monkeypatch.setenv(bench._VERIFIED_TOOL_ROOT_ENV, str(tool_root))  # noqa: SLF001
    monkeypatch.setenv(bench._VERIFIED_TOOL_CAPABILITY_ENV, capability)  # noqa: SLF001
    monkeypatch.setenv(  # noqa: SLF001
        bench._COMMITTED_EVALUATOR_ENV,
        str(manifest["evaluator_blob_sha256"]),
    )

    with pytest.raises(bench._ClosedArmError, match="committed_tool_root_shape_invalid"):
        bench._verified_tool_root()  # noqa: SLF001


def test_isolated_arm_uses_stdin_allowlist_and_fail_closed_scratch_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("FRIDAY_DATABASE_PATH", "/sentinel/live.sqlite3")
    monkeypatch.setenv("JERICHO_HOME", "/sentinel/live-home")
    monkeypatch.setenv("FRIDAY_API_TOKEN", "must-not-cross")
    positive_ids = {
        case.id for case in bench.GOLD_CASES if case.split == "calibration" and case.expected_knowledge_ids
    }

    def fake_subprocess(argv, *, cwd, environment, input_data, timeout):
        request = json.loads(input_data)
        outcome = _synthetic_arm("candidate", "calibration", positive_hits=positive_ids)
        outcome.pop("control")
        outcome["request_id"] = hashlib.sha256(request["nonce"].encode()).hexdigest()
        captured.update(
            argv=argv,
            cwd=cwd,
            environment=environment,
            request=request,
            timeout=timeout,
        )
        return subprocess.CompletedProcess(argv, 0, json.dumps(outcome).encode(), b"")

    monkeypatch.setattr(bench, "_run_arm_subprocess", fake_subprocess)
    monkeypatch.setattr(bench, "_verified_tool_root", lambda: tmp_path)
    settings = {
        "FRIDAY_LLM_ENABLED": "0",
        "FRIDAY_LLM_API_KEY": "synthetic-secret",
        "FRIDAY_RERANK_TOP": "40",
    }

    outcome = bench._invoke_isolated_arm(  # noqa: SLF001
        tmp_path,
        arm="candidate",
        split="calibration",
        include_control=False,
        model_settings=settings,
    )

    assert outcome["ok"] is True
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert "PYTHONPATH" not in environment
    assert "FRIDAY_LLM_API_KEY" not in environment
    assert "FRIDAY_API_TOKEN" not in environment
    assert Path(environment["FRIDAY_DATABASE_PATH"]).is_relative_to(Path(captured["cwd"]))
    assert Path(environment["FRIDAY_ENV_FILE"]).is_relative_to(Path(captured["cwd"]))
    assert not Path(environment["FRIDAY_ENV_FILE"]).exists()
    assert environment[bench._ARM_TOOL_ROOT_ENV] == str(tmp_path.resolve())  # noqa: SLF001
    assert environment[bench._ARM_TOOL_ROOT_ENV] != str((bench.ROOT / "tools").resolve())  # noqa: SLF001
    assert environment[bench._REPO_ROOT_ENV] == str(bench.ROOT)  # noqa: SLF001
    assert captured["request"]["settings"] == settings
    assert "-I" in captured["argv"] and "-B" in captured["argv"]


@pytest.mark.parametrize(
    ("mode", "expected_stage"),
    (
        ("stderr", "arm_stderr_nonempty"),
        ("exit", "arm_exit_nonzero"),
        ("garbage", "arm_stdout_invalid"),
        ("timeout", "arm_timeout"),
    ),
)
def test_isolated_arm_transport_failures_never_echo_child_contents(
    tmp_path: Path,
    monkeypatch,
    mode: str,
    expected_stage: str,
) -> None:
    def fake_subprocess(argv, *, cwd, environment, input_data, timeout):
        del cwd, environment, timeout
        request = json.loads(input_data)
        if mode == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        if mode == "stderr":
            return subprocess.CompletedProcess(argv, 0, b"{}", b"private backend details")
        if mode == "garbage":
            return subprocess.CompletedProcess(argv, 0, b"not-json private details", b"")
        envelope = {
            "protocol": bench._ARM_PROTOCOL,  # noqa: SLF001
            "request_id": hashlib.sha256(request["nonce"].encode()).hexdigest(),
            "ok": False,
            "stage": "arm_import_incompatible",
        }
        return subprocess.CompletedProcess(argv, 3, json.dumps(envelope).encode(), b"")

    monkeypatch.setattr(bench, "_run_arm_subprocess", fake_subprocess)
    monkeypatch.setattr(bench, "_verified_tool_root", lambda: tmp_path)
    outcome = bench._invoke_isolated_arm(  # noqa: SLF001
        tmp_path,
        arm="exact_base",
        split="calibration",
        include_control=False,
        model_settings={"FRIDAY_LLM_ENABLED": "0"},
    )

    assert outcome["ok"] is False
    assert outcome["stage"] == expected_stage
    assert "private" not in json.dumps(outcome)


def test_internal_holdout_entry_checks_the_seal_before_any_arm_work(monkeypatch, capsys) -> None:
    nonce = "a" * 64
    request = {
        "protocol": bench._ARM_PROTOCOL,  # noqa: SLF001
        "nonce": nonce,
        "arm": "exact_base",
        "split": "holdout",
        "include_control": True,
        "settings": {"FRIDAY_LLM_ENABLED": "0"},
    }

    class _Input:
        buffer = io.BytesIO(json.dumps(request).encode())

    monkeypatch.setattr(sys, "stdin", _Input())
    monkeypatch.setenv(bench._ARM_NONCE_ENV, nonce)  # noqa: SLF001
    monkeypatch.setattr(bench, "audit_gold_set", lambda: [])
    monkeypatch.setattr(bench, "candidate_manifest_complaints", lambda: ["sealed"])
    monkeypatch.setattr(
        bench,
        "_execute_synthetic_arm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("arm reached")),
    )

    assert bench._internal_arm_process() == bench.BASELINE_EXIT_CONTRACT_INVALID  # noqa: SLF001
    payload = json.loads(capsys.readouterr().out)
    assert payload["stage"] == "holdout_seal_closed"


def test_holdout_attempt_latch_is_atomic_bound_and_strictly_one_shot(tmp_path: Path, monkeypatch) -> None:
    exact_diff = b"candidate diff"
    manifest = _candidate_manifest(exact_diff)
    monkeypatch.setattr(bench, "_git_common_dir", lambda: tmp_path)
    monkeypatch.setattr(bench, "_load_candidate_manifest", lambda: manifest)

    assert bench._consume_holdout_attempt() is None  # noqa: SLF001
    marker = tmp_path / bench._HOLDOUT_LATCH_NAME  # noqa: SLF001
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload == {
        "contract": "temporal_holdout_attempt_v1",
        "candidate_id": bench.CANDIDATE_ID,
        "base_commit": bench.CANDIDATE_BASE_COMMIT,
        "gold_manifest_sha256": bench.GOLD_MANIFEST_SHA256,
        "candidate_diff_sha256": hashlib.sha256(exact_diff).hexdigest(),
        "evaluator_blob_sha256": manifest["evaluator_blob_sha256"],
        "helper_blob_sha256": manifest["helper_blob_sha256"],
    }
    assert marker.stat().st_mode & 0o777 == 0o600
    assert bench._consume_holdout_attempt() == "holdout_attempt_already_consumed"  # noqa: SLF001


def test_consumed_holdout_never_reaches_model_settings_or_an_arm(monkeypatch, capsys) -> None:
    monkeypatch.setattr(bench, "audit_gold_set", lambda: [])
    monkeypatch.setattr(bench, "candidate_manifest_complaints", lambda: [])
    monkeypatch.setattr(
        bench,
        "_consume_holdout_attempt",
        lambda: "holdout_attempt_already_consumed",
    )
    monkeypatch.setattr(
        bench,
        "_model_runtime_settings",
        lambda: (_ for _ in ()).throw(AssertionError("model settings reached after latch")),
    )

    assert bench._run_paired_holdout() == bench.BASELINE_EXIT_CONTRACT_INVALID  # noqa: SLF001
    assert json.loads(capsys.readouterr().out)["stage"] == "holdout_attempt_already_consumed"


def test_exact_base_calibration_requires_the_published_numbers_and_proven_runtime() -> None:
    report = _synthetic_measurement_report(
        "calibration",
        positive_hits=set(),
        rerank_applied_ids={case.id for case in bench.GOLD_CASES if case.split == "calibration"},
    )
    arm = _synthetic_arm("exact_base", "calibration", positive_hits=set())

    accepted = bench.exact_base_calibration_acceptance(
        report,
        runtime=arm["runtime"],
        provenance=arm["provenance"],
    )
    assert accepted["accepted"] is True

    no_calls = copy.deepcopy(report)
    no_calls["reranker_calls"] = 15
    assert (
        bench.exact_base_calibration_acceptance(
            no_calls,
            runtime=arm["runtime"],
            provenance=arm["provenance"],
        )["checks"]["production_reranker_proven"]
        is False
    )

    not_applied = copy.deepcopy(report)
    not_applied["rerank_applied_cases"] = 15
    assert (
        bench.exact_base_calibration_acceptance(
            not_applied,
            runtime=arm["runtime"],
            provenance=arm["provenance"],
        )["checks"]["production_reranker_proven"]
        is False
    )

    wrong_depth = copy.deepcopy(arm["runtime"])
    wrong_depth["rerank_top"] = 20
    assert (
        bench.exact_base_calibration_acceptance(
            report,
            runtime=wrong_depth,
            provenance=arm["provenance"],
        )["checks"]["production_reranker_proven"]
        is False
    )

    incomplete = copy.deepcopy(arm["runtime"])
    incomplete["embedding_index_complete"] = False
    assert (
        bench.exact_base_calibration_acceptance(
            report,
            runtime=incomplete,
            provenance=arm["provenance"],
        )["checks"]["embedding_index_complete"]
        is False
    )

    wrong_provenance = copy.deepcopy(arm["provenance"])
    wrong_provenance["package"] = "working_tree"
    assert (
        bench.exact_base_calibration_acceptance(
            report,
            runtime=arm["runtime"],
            provenance=wrong_provenance,
        )["checks"]["archive_only_provenance"]
        is False
    )


def _accepted_candidate_calibration_arm() -> dict:
    positive_ids = {
        case.id for case in bench.GOLD_CASES if case.split == "calibration" and case.expected_knowledge_ids
    }
    return _synthetic_arm("candidate", "calibration", positive_hits=positive_ids)


def test_candidate_calibration_requires_production_runtime_and_quality() -> None:
    arm = _accepted_candidate_calibration_arm()
    acceptance = bench.candidate_calibration_acceptance(
        arm["report"],
        runtime=arm["runtime"],
        provenance=arm["provenance"],
    )

    assert acceptance["accepted"] is True


@pytest.mark.parametrize(
    ("target", "field", "value", "failed_check"),
    (
        ("runtime", "embeddings_remote_enabled", False, "remote_embeddings_proven"),
        ("runtime", "embedding_index_complete", False, "embedding_index_complete"),
        ("runtime", "embedding_object_vectors", 0, "embedding_index_complete"),
        ("runtime", "reranker_configured", False, "production_reranker_proven"),
        ("runtime", "rerank_top", 20, "production_reranker_proven"),
        ("runtime", "rerank_confident_min", 0.0, "production_reranker_proven"),
        ("runtime", "record_usage", True, "usage_disabled"),
        ("report", "graph_failures", 1, "zero_infrastructure_failures"),
        ("report", "structure_unchanged", False, "structure_unchanged"),
        ("provenance", "package", "working_tree", "candidate_archive_provenance"),
        ("report", "expected_hits_at_10", 7, "minimum_expected_hits_at_10"),
    ),
)
def test_each_candidate_calibration_prerequisite_fails_closed(
    target: str,
    field: str,
    value: object,
    failed_check: str,
) -> None:
    arm = _accepted_candidate_calibration_arm()
    arm[target][field] = value

    acceptance = bench.candidate_calibration_acceptance(
        arm["report"],
        runtime=arm["runtime"],
        provenance=arm["provenance"],
    )

    assert acceptance["accepted"] is False
    assert failed_check in acceptance["failed_checks"]


def test_exact_base_parent_deletes_the_archive_tree_after_calibration(monkeypatch) -> None:
    seen: dict[str, Path] = {}

    def fake_extract(destination: Path) -> None:
        seen["tree"] = destination

    def fake_invoke(package_root: Path, **kwargs):
        assert package_root == seen["tree"]
        assert kwargs == {
            "arm": "exact_base",
            "split": "calibration",
            "include_control": False,
        }
        outcome = _synthetic_arm("exact_base", "calibration", positive_hits=set())
        outcome.pop("control")
        return outcome

    monkeypatch.setattr(bench, "audit_gold_set", lambda: [])
    monkeypatch.setattr(bench, "_extract_exact_base_friday", fake_extract)
    monkeypatch.setattr(bench, "_invoke_isolated_arm", fake_invoke)

    outcome = bench._run_exact_base_calibration()  # noqa: SLF001

    assert outcome["ok"] is True
    assert not seen["tree"].exists()


@pytest.mark.parametrize(
    ("field", "value", "failed_check"),
    (
        ("cases", 19, "exact_case_counts"),
        ("correct", 5, "exact_correct_at_10"),
        ("expected_hits_at_10", 1, "exact_expected_hits_at_10"),
        ("no_answer_correct", 3, "exact_no_answer"),
        ("forbidden_hits_at_10", 1, "exact_forbidden_hits_at_10"),
        ("mrr", 0.0625, "exact_mrr"),
        ("graph_failures", 1, "zero_infrastructure_failures"),
        ("structure_unchanged", False, "zero_infrastructure_failures"),
    ),
)
def test_every_exact_base_calibration_number_is_frozen(
    field: str,
    value: object,
    failed_check: str,
) -> None:
    report = _synthetic_measurement_report(
        "calibration",
        positive_hits=set(),
        rerank_applied_ids={case.id for case in bench.GOLD_CASES if case.split == "calibration"},
    )
    report[field] = value
    arm = _synthetic_arm("exact_base", "calibration", positive_hits=set())

    acceptance = bench.exact_base_calibration_acceptance(
        report,
        runtime=arm["runtime"],
        provenance=arm["provenance"],
    )

    assert acceptance["accepted"] is False
    assert failed_check in acceptance["failed_checks"]
    incomplete = copy.deepcopy(arm["runtime"])
    incomplete["embedding_index_complete"] = False
    assert (
        bench.exact_base_calibration_acceptance(
            report,
            runtime=incomplete,
            provenance=arm["provenance"],
        )["checks"]["embedding_index_complete"]
        is False
    )


def test_paired_holdout_reports_exact_deltas_and_accepts_a_clean_gain() -> None:
    positive_ids = [
        case.id for case in bench.GOLD_CASES if case.split == "holdout" and case.expected_knowledge_ids
    ]
    baseline = _synthetic_arm("exact_base", "holdout", positive_hits=set())
    candidate = _synthetic_arm("candidate", "holdout", positive_hits=set(positive_ids[:2]))

    report = bench.compare_holdout_arms(baseline, candidate)

    assert report["comparison"] == {
        "wins": 2,
        "losses": 0,
        "net": 2,
        "win_case_ids": positive_ids[:2],
        "loss_case_ids": [],
        "expected_hits_at_10_delta": 2,
        "forbidden_hits_at_10_delta": 0,
        "mrr_delta": 0.125,
    }
    assert report["non_temporal_control"]["byte_identical"] is True
    assert report["acceptance"]["accepted"] is True
    assert bench._holdout_exit_code(report) == 0  # noqa: SLF001


@pytest.mark.parametrize(
    ("path", "value", "failed_check", "exit_code"),
    (
        (("comparison", "net"), 1, "minimum_net_gain", 4),
        (("comparison", "losses"), 1, "zero_losses", 4),
        (("baseline", "summary", "expected_hits_at_10"), 3, "expected_hits_not_lower", 4),
        (("candidate", "summary", "forbidden_hits_at_10"), 1, "forbidden_hits_not_higher", 4),
        (("candidate", "summary", "mrr"), -0.02, "mrr_within_floor", 4),
        (("infrastructure", "both_valid"), False, "zero_infrastructure_failures", 3),
        (("infrastructure", "candidate_structure_unchanged"), False, "structures_unchanged", 3),
        (("non_temporal_control", "byte_identical"), False, "non_temporal_control_byte_identical", 4),
    ),
)
def test_every_holdout_acceptance_guard_is_enforced(
    path: tuple[str, ...],
    value: object,
    failed_check: str,
    exit_code: int,
) -> None:
    positive_ids = [
        case.id for case in bench.GOLD_CASES if case.split == "holdout" and case.expected_knowledge_ids
    ]
    report = bench.compare_holdout_arms(
        _synthetic_arm("exact_base", "holdout", positive_hits=set()),
        _synthetic_arm("candidate", "holdout", positive_hits=set(positive_ids[:2])),
    )
    target = report
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    acceptance = bench.holdout_acceptance(report)

    assert acceptance["accepted"] is False
    assert failed_check in acceptance["failed_checks"]
    assert bench._holdout_exit_code(report) == exit_code  # noqa: SLF001


def test_non_temporal_ranking_projection_must_be_byte_identical() -> None:
    positive_ids = [
        case.id for case in bench.GOLD_CASES if case.split == "holdout" and case.expected_knowledge_ids
    ]
    baseline = _synthetic_arm("exact_base", "holdout", positive_hits=set())
    candidate = _synthetic_arm("candidate", "holdout", positive_hits=set(positive_ids[:2]))
    changed_case = candidate["control"]["projection"][0]["case"]
    candidate["control"]["projection"][0]["result_ids"].append("ko-temporal-control-mutation")
    candidate["control"]["projection_sha256"] = hashlib.sha256(
        bench._control_projection_bytes(candidate["control"])  # noqa: SLF001
    ).hexdigest()

    report = bench.compare_holdout_arms(baseline, candidate)

    assert report["non_temporal_control"]["byte_identical"] is False
    assert report["non_temporal_control"]["mismatch_case_ids"] == [changed_case]
    assert report["acceptance"]["accepted"] is False
    assert bench._holdout_exit_code(report) == bench.BASELINE_EXIT_QUALITY_REJECTED  # noqa: SLF001
