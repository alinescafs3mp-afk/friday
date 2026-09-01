from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from friday.retrieval_benchmark.release import archive_search_release_sha256

_ROOT = Path(__file__).resolve().parents[2]
_INSTRUMENT = _ROOT / "tools" / "document_dense_recall_measurement.py"
_EVIDENCE = _ROOT / "evidence" / "s4_document_dense_recall_before_after.json"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _stable_measurement(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in (
            "claim",
            "corpus",
            "index_fixture",
            "instrument_sha256",
            "limitations",
            "model_fixture",
            "network_forbidden",
            "release_sha256",
            "result",
            "schema",
        )
    }


def _run_candidate() -> tuple[dict[str, Any], bytes]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(_ROOT)
    completed = subprocess.run(  # noqa: S603
        [sys.executable, str(_INSTRUMENT), "--arm", "dense"],
        cwd=_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        timeout=120,
    )
    assert completed.stderr == b""
    payload = json.loads(completed.stdout)
    assert type(payload) is dict
    return payload, completed.stdout


def test_current_dense_report_reproduces_the_exact_body_free_candidate() -> None:
    evidence = json.loads(_EVIDENCE.read_text(encoding="utf-8"))
    measured, raw_output = _run_candidate()
    candidate = evidence["candidate"]

    assert evidence["schema"] == "friday.s4-document-dense-recall.before-after.body-free.v2"
    assert measured["schema"] == "friday.document-dense-recall-measurement.body-free.v1"
    assert measured["corpus"] == {
        key: value for key, value in evidence["corpus"].items() if key != "projection"
    }
    assert measured["release_sha256"] == candidate["release_sha256"]
    assert measured["release_sha256"] == archive_search_release_sha256()
    assert measured["instrument_sha256"] == evidence["instrument"]["instrument_sha256"]
    assert measured["network_forbidden"] is True
    assert measured["claim"] == evidence["claim"]
    assert measured["index_fixture"] == evidence["index_fixture"]
    assert measured["limitations"] == evidence["limitations"]
    assert measured["source"]["worktree_clean"] is True
    assert measured["source"]["worktree_status_sha256"] == hashlib.sha256(b"").hexdigest()
    guard = measured["source"]["guard"]
    assert guard["release_sha256_start"] == guard["release_sha256_end"] == measured["release_sha256"]
    assert guard["instrument_sha256_start"] == guard["instrument_sha256_end"] == measured["instrument_sha256"]
    assert measured["result"]["arm"] == "dense"
    assert measured["result"]["metrics"] == candidate["metrics"]
    assert measured["result"]["per_kind_recall_at_10"] == candidate["per_kind_recall_at_10"]
    assert measured["result"]["authorized_foreign_sources_returned"] == 0
    assert measured["result"]["current_revision_cases"] == 24
    assert measured["result"]["dense_evidence_cases"] == 24
    assert _sha256(_stable_measurement(measured)) == candidate["stable_envelope_sha256"]
    assert _sha256(measured["result"]) == candidate["result_sha256"]
    assert _sha256(measured["result"]["cases"]) == candidate["case_results_sha256"]
    assert raw_output == _canonical(measured) + b"\n"
    assert evidence["instrument"]["candidate_repeat_output_identical"] is True
    assert len(evidence["instrument"]["candidate_repeat_output_sha256"]) == 64


def test_dense_report_is_truthful_about_scope_and_context_only_observations() -> None:
    evidence = json.loads(_EVIDENCE.read_text(encoding="utf-8"))
    baseline = evidence["baseline"]
    candidate = evidence["candidate"]
    comparison = evidence["comparison"]

    assert evidence["gate"] == {
        "canonical_requirement": "outer_sol/PROJECT_BACKLOG.md S4 item 4",
        "disposition": "current_code_owned_synthetic_corpus_established_before_candidate_comparison",
        "production_embedding_model_quality": "not_measured",
        "production_owner_corpus": "not_measured",
        "release_threshold": "not_assessed",
    }
    assert evidence["limitations"] == [
        "frozen_qrel_axis_vectors_measure_archive_federation_ranking_and_reauthorization_not_production_embedding_quality",
        "private_federation_capture_does_not_measure_execution_kernel_or_model_visible_output",
        "long_form_projection_preserves_current_24_qrels_but_is_synthetic_not_live_owner_corpus",
        "production_embedding_model_quality_not_measured",
        "absence_is_not_claimed_from_the_dense_lane",
    ]
    assert evidence["claim"] == {
        "corpus": "current_code_owned_synthetic_long_form_projection",
        "execution_kernel_path": "not_measured",
        "model_visible_output": "not_measured",
        "production_embedding_model_quality": "not_measured",
        "production_owner_corpus": "not_measured",
        "scope": "synthetic_archive_federation_ranking_and_reauthorization",
    }
    assert baseline["source_worktree_clean"] is True
    assert candidate["source_worktree_clean"] is True
    assert baseline["source_guard"]["release_sha256_start"] == baseline["source_guard"]["release_sha256_end"]
    assert (
        candidate["source_guard"]["release_sha256_start"] == candidate["source_guard"]["release_sha256_end"]
    )
    assert comparison["recall_at_10_numerator_delta"] == (
        candidate["metrics"]["recall_at_10"]["numerator"] - baseline["metrics"]["recall_at_10"]["numerator"]
    )
    assert comparison["recall_at_20_numerator_delta"] == (
        candidate["metrics"]["recall_at_20"]["numerator"] - baseline["metrics"]["recall_at_20"]["numerator"]
    )
    assert comparison["regression"] is False
    observations = evidence["context_only_observations"]
    assert observations["embedding_backend_benchmark"] == {
        "case_count": 24,
        "classification": (
            "body_free_on_demand_model_benchmark_not_archive_federation_owner_corpus_or_release_evidence"
        ),
        "dense_recall_at_10": {"denominator": 24, "numerator": 20, "ppm": 833333},
        "document_count": 140,
        "embedding_failures": 0,
        "lexical_recall_at_10": {"denominator": 24, "numerator": 14, "ppm": 583333},
        "model_id": "qwen3-embedding-0.6b",
        "recall_at_10_delta_ppm": 250000,
    }
    assert observations["production_index_coverage"] == {
        "classification": "read_only_coverage_only_not_recall_quality_or_release_evidence",
        "embedding_identity": {
            "chunk_scheme": "v2:1200:200:64",
            "dimensions": 1024,
            "model_id": "qwen3-embedding-0.6b",
        },
        "knowledge_objects_with_vectors": 1088,
        "object_vectors": 1570,
        "passage_vectors": 14094,
        "schema_version": 50,
    }


def test_checked_in_measurement_contains_no_private_corpus_material() -> None:
    import tools.retrieval_bench as corpus

    serialized = _EVIDENCE.read_text(encoding="utf-8")
    forbidden = {
        '"query":',
        "/home/",
        "/tmp/",
        "document-dense-measurement-tenant",
        "document-dense-measurement-principal",
        "document-dense-measurement-foreign",
        "dense-measurement:",
        "raw_c",
        "ko_dense_measure_",
    }
    for document_id, title, body, _category in corpus.DOCUMENTS:
        forbidden.update((document_id, title, body))
    for query, expected, _kind in corpus.GOLD:
        forbidden.update((query, expected))
    assert all(value not in serialized for value in forbidden)
