"""Measured S10b boundary for explicit dismissal of a relational mention."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

import friday.retrieval as retrieval
from friday.retrieval import is_relational_query


@pytest.mark.parametrize(
    "query",
    [
        "Вообще мне не важно: связан ли узел с контуром.",
        "Неважно: связан ли этот блок с контуром.",
        "Меня не интересует — участвует ли группа в проекте.",
        "Мне не нужен: с кем работал координатор.",
        "Мне не надо, работать над модулем.",
        "Я не спрашиваю: относится к системе этот блок.",
        "Без разницы — часть проекта это или отдельный сервис.",
    ],
)
def test_an_explicitly_dismissed_relational_mention_does_not_enable_the_graph(query: str) -> None:
    assert is_relational_query(query) is False


@pytest.mark.parametrize(
    "query",
    [
        "Мне не важно, какой выбран цвет; уточни, с кем работает группа.",
        "Меня не интересует срок, но от чего зависит запуск?",
        "Без разницы, где лежит отчёт — покажи, какие блоки связаны с контуром.",
    ],
)
def test_a_dismissal_of_another_subject_does_not_hide_the_real_request(query: str) -> None:
    assert is_relational_query(query) is True


def test_a_later_live_relational_match_survives_an_earlier_dismissed_one() -> None:
    query = "Не важно, с кем работал первый участник. Покажи, что связано со вторым модулем."

    assert is_relational_query(query) is True


def test_clause_slicing_is_equivalent_to_the_frozen_full_prefix_candidate() -> None:
    beginnings = ("", "Не важно: ", "Вводная часть. Не интересно — ", "Сначала уточнение; ")
    relations = ("связан ли блок", "с кем работал отдел", "часть проекта")
    endings = (".", "; затем обычная просьба", "\nБез разницы — участвует ли группа")

    queries = [
        f"{beginning}{relation}{ending}"
        for beginning in beginnings
        for relation in relations
        for ending in endings
    ]
    queries.extend(
        [
            "Не важно,\nс кем работал узел",
            "Мне не надо \n\n связан ли блок",
            "Вводная. Не интересует\nучаствует ли группа",
        ]
    )
    for query in queries:
        reference = any(
            retrieval._DISMISSED_RELATIONAL_PREFIX_RE.search(query[: match.start()]) is None
            for match in retrieval._RELATIONAL_QUERY_RE.finditer(query)
        )
        assert is_relational_query(query) is reference


def test_many_dismissed_matches_do_not_rescan_the_growing_full_prefix(monkeypatch) -> None:
    real_pattern = retrieval._DISMISSED_RELATIONAL_PREFIX_RE
    inspected_windows: list[tuple[int, int]] = []

    class _PrefixProbe:
        @staticmethod
        def search(query: str, start: int, end: int):
            inspected_windows.append((start, end))
            return real_pattern.search(query, start, end)

    monkeypatch.setattr(retrieval, "_DISMISSED_RELATIONAL_PREFIX_RE", _PrefixProbe())
    query = "Не важно: связан. " * 2_000

    assert retrieval.is_relational_query(query) is False
    assert len(inspected_windows) == 2_000
    assert all(
        current[0] >= previous[1]
        for previous, current in zip(inspected_windows, inspected_windows[1:], strict=False)
    )
    assert max(end - start for start, end in inspected_windows) < 32


def test_the_deidentified_s10b_result_is_self_consistent_and_passes_its_frozen_gate() -> None:
    path = (
        Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "relational_classifier_deidentified.json"
    )
    assert path.stat().st_size <= 64 * 1024
    result = json.loads(path.read_text(encoding="utf-8"))
    assert set(result) == {"cases", "candidate", "dataset", "per_case", "summary"}
    assert result["cases"] == 12
    assert result["candidate"] == "dismiss_explicit_prefix_v1"
    expected_dataset = {
        "selection_frozen_before_arms": True,
        "labels_frozen_before_arms": True,
        "human_authored_only": True,
        "synthetic_document_notices": False,
        "language": "ru",
    }
    assert result["dataset"] == expected_dataset
    assert all(isinstance(result["dataset"][key], bool) for key in expected_dataset if key != "language")
    assert type(result["dataset"]["language"]) is str

    rows = result["per_case"]
    row_keys = {
        "case",
        "class",
        "expected_relational",
        "baseline_match",
        "candidate_match",
        "relational_match_count",
        "dismissed_match_count",
    }
    assert isinstance(rows, list) and len(rows) == 12
    assert all(isinstance(row, dict) and set(row) == row_keys for row in rows)
    assert len({row["case"] for row in rows}) == 12
    assert all(re.fullmatch(r"[0-9a-f]{16}", row["case"]) for row in rows)
    assert Counter(row["class"] for row in rows) == {
        "direct_relational_request": 3,
        "prefaced_relational_request": 3,
        "dismissed_relational_mention": 3,
        "redirected_nonrelational_request": 3,
    }
    assert all(row["baseline_match"] is True for row in rows)
    positive_classes = {"direct_relational_request", "prefaced_relational_request"}
    for row in rows:
        assert isinstance(row["expected_relational"], bool)
        assert row["expected_relational"] is (row["class"] in positive_classes)
        assert isinstance(row["candidate_match"], bool)
        matches = row["relational_match_count"]
        dismissed = row["dismissed_match_count"]
        assert isinstance(matches, int) and not isinstance(matches, bool) and matches > 0
        assert isinstance(dismissed, int) and not isinstance(dismissed, bool)
        assert 0 <= dismissed <= matches
        assert row["candidate_match"] is (dismissed < matches)

    def matrix(field: str) -> tuple[int, int, int, int]:
        tp = sum(row["expected_relational"] and row[field] for row in rows)
        fp = sum(not row["expected_relational"] and row[field] for row in rows)
        tn = sum(not row["expected_relational"] and not row[field] for row in rows)
        fn = sum(row["expected_relational"] and not row[field] for row in rows)
        return tp, fp, tn, fn

    btp, bfp, btn, bfn = matrix("baseline_match")
    ctp, cfp, ctn, cfn = matrix("candidate_match")
    fixed = sum(
        not row["expected_relational"] and row["baseline_match"] and not row["candidate_match"]
        for row in rows
    )
    new_false_negatives = sum(
        row["expected_relational"] and row["baseline_match"] and not row["candidate_match"] for row in rows
    )
    expected_summary = {
        "true_positives_baseline": btp,
        "false_positives_baseline": bfp,
        "true_negatives_baseline": btn,
        "false_negatives_baseline": bfn,
        "true_positives_candidate": ctp,
        "false_positives_candidate": cfp,
        "true_negatives_candidate": ctn,
        "false_negatives_candidate": cfn,
        "fixed_false_positives": fixed,
        "new_false_negatives": new_false_negatives,
        "correct_baseline": btp + btn,
        "correct_candidate": ctp + ctn,
        "net_corrections": fixed - new_false_negatives,
    }
    assert all(isinstance(value, int) and not isinstance(value, bool) for value in result["summary"].values())
    assert result["summary"] == expected_summary
    assert (btp, bfp, btn, bfn) == (6, 6, 0, 0)
    assert ctp == 6 and cfn == 0
    assert fixed >= 4 and expected_summary["net_corrections"] >= 4
