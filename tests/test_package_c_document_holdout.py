"""Package C document acceptance was frozen before production changes.

Every value is synthetic.  The byte hash and preregistered shape prevent a
later implementation from shrinking the hard cases, relabelling a failure, or
quietly replacing the corpus with examples copied from a private conversation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "package_c_document_holdout.json"
FIXTURE_SHA256 = "1f15596978bc839030744c03dc1380ca456b9c91a7b4220c520295a2e9ffeeb9"

EXPECTED_ENUMS = {
    "defect": ["K05", "K06", "K07", "K19"],
    "format": ["csv", "docx", "txt", "xlsx"],
    "exact_kind": ["count_people", "count_records", "list_people", "list_records", "none"],
    "record_set_kind": ["person_rows", "table_rows", "none"],
    "expected_status": ["passed", "unknown", "model_preserved", "empty_notice"],
    "empty_source": ["current_turn", "idempotent_replay", "restored_conversation"],
}

EXPECTED_ACCEPTANCE = {
    "expected_case_counts": {
        "k19_ordinary_tables": 8,
        "k19_controls": 6,
        "k05_preservation_cases": 10,
        "k05_rejection_controls": 6,
        "k06_human_refusals": 4,
        "k07_empty_cases": 5,
        "k07_controls": 5,
        "total": 44,
    },
    "all_ids_must_be_unique": True,
    "all_questions_must_be_unique_within_group": True,
    "ordinary_record_sets_are_code_owned": True,
    "ordinary_model_answers_are_preserved_byte_for_byte": True,
    "exact_failures_name_the_user_problem_and_offer_a_next_step": True,
    "successful_empty_extraction_is_not_an_unreadable_file": True,
    "banned_user_facing_fragments": [
        "проверяемым кодовым путём",
        "структурного индекса",
        "в текущем ходе",
        "office-файла",
    ],
    "required_empty_notice": "Текста в файле не оказалось",
}

GROUPS = (
    "k19_ordinary_tables",
    "k19_controls",
    "k05_preservation_cases",
    "k05_rejection_controls",
    "k06_human_refusals",
    "k07_empty_cases",
    "k07_controls",
)


def load_package_c_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def package_c_cases(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    return [case for group in GROUPS for case in fixture[group]]


def test_package_c_fixture_bytes_are_frozen_before_implementation() -> None:
    assert hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() == FIXTURE_SHA256


def test_package_c_fixture_is_declared_synthetic_and_contains_no_live_payload_section() -> None:
    fixture = load_package_c_fixture()

    assert set(fixture) == {
        "$schema",
        "schema_version",
        "synthetic_only",
        "purpose",
        "privacy",
        "closed_enums",
        "acceptance",
        *GROUPS,
    }
    assert fixture["$schema"] == "friday.package_c_document_holdout.v1"
    assert fixture["schema_version"] == 1
    assert fixture["synthetic_only"] is True
    assert fixture["privacy"] == {
        "contains_live_chat": False,
        "contains_live_file_text": False,
        "contains_real_people": False,
        "contains_real_tenant_ids": False,
        "contains_model_responses": False,
    }
    encoded = json.dumps(fixture, ensure_ascii=False).casefold()
    for forbidden_key in (
        '"live_chat"',
        '"raw_chat"',
        '"raw_utterance"',
        '"raw_response"',
        '"tenant_id"',
    ):
        assert forbidden_key not in encoded


def test_package_c_preregistered_enums_acceptance_and_counts_do_not_drift() -> None:
    fixture = load_package_c_fixture()

    assert fixture["closed_enums"] == EXPECTED_ENUMS
    assert fixture["acceptance"] == EXPECTED_ACCEPTANCE
    observed = {group: len(fixture[group]) for group in GROUPS}
    assert observed == {
        key: value for key, value in EXPECTED_ACCEPTANCE["expected_case_counts"].items() if key != "total"
    }
    assert sum(observed.values()) == EXPECTED_ACCEPTANCE["expected_case_counts"]["total"]


def test_package_c_has_44_unique_ids_and_unique_questions_within_each_group() -> None:
    fixture = load_package_c_fixture()
    cases = package_c_cases(fixture)
    identifiers = [str(case["id"]) for case in cases]

    assert len(cases) == 44
    assert len(set(identifiers)) == len(identifiers)
    for group in GROUPS:
        questions = [
            " ".join(str(case["question"]).casefold().split())
            for case in fixture[group]
            if "question" in case
        ]
        assert all(questions)
        assert len(questions) == len(set(questions))


def test_package_c_labels_stay_inside_closed_enums() -> None:
    fixture = load_package_c_fixture()

    for case in package_c_cases(fixture):
        assert case["defect"] in EXPECTED_ENUMS["defect"]
        if "format" in case:
            assert case["format"] in EXPECTED_ENUMS["format"]
        if "source" in case:
            assert case["source"] in EXPECTED_ENUMS["empty_source"]
        if "exact_kind" in case:
            assert case["exact_kind"] in EXPECTED_ENUMS["exact_kind"]
        expected = case.get("expected")
        if isinstance(expected, dict):
            assert expected["status"] in EXPECTED_ENUMS["expected_status"]
            assert expected["exact_kind"] in EXPECTED_ENUMS["exact_kind"]
            assert expected["record_set_kind"] in EXPECTED_ENUMS["record_set_kind"]
        if "expected_status" in case:
            assert case["expected_status"] in EXPECTED_ENUMS["expected_status"]


def test_package_c_table_rows_and_outputs_are_wholly_synthetic() -> None:
    fixture = load_package_c_fixture()
    table_cases = [*fixture["k19_ordinary_tables"], *fixture["k19_controls"]]

    assert all(case["rows"] and all(isinstance(row, list) for row in case["rows"]) for case in table_cases)
    assert all(
        str(value).strip() for case in table_cases for row in case["rows"] for value in row if value != ""
    )
    assert all(
        str(case["synthetic_model_answer"]).strip()
        for case in [*fixture["k05_preservation_cases"], *fixture["k05_rejection_controls"]]
    )


def test_package_c_case_id_ranges_and_defect_ownership_are_fixed() -> None:
    fixture = load_package_c_fixture()
    expected_ids = {
        "k19_ordinary_tables": [f"k19_table_{number:03d}" for number in range(1, 9)],
        "k19_controls": [f"k19_control_{number:03d}" for number in range(1, 7)],
        "k05_preservation_cases": [f"k05_preserve_{number:03d}" for number in range(1, 11)],
        "k05_rejection_controls": [f"k05_reject_{number:03d}" for number in range(1, 7)],
        "k06_human_refusals": [f"k06_refusal_{number:03d}" for number in range(1, 5)],
        "k07_empty_cases": [f"k07_empty_{number:03d}" for number in range(1, 6)],
        "k07_controls": [f"k07_control_{number:03d}" for number in range(1, 6)],
    }

    for group, identifiers in expected_ids.items():
        assert [case["id"] for case in fixture[group]] == identifiers
        defect = group.split("_", 1)[0].upper()
        assert {case["defect"] for case in fixture[group]} == {defect}
