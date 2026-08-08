"""Package B routing acceptance is frozen before production code changes.

The fixture is wholly synthetic.  These tests deliberately pin both its bytes
and its preregistered shape so a later implementation cannot quietly edit the
questions, labels, controls, or acceptance counts to fit its own behaviour.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "package_b_routing_holdout.json"
FIXTURE_SHA256 = "87cd0083596a865e1e6a3ef5a238955654ef362f129e0561485b8af9ad7faa00"

EXPECTED_ENUMS = {
    "case_group": [
        "time_positive",
        "time_control",
        "global_count_positive",
        "local_count_control",
    ],
    "time_direction": ["past", "future", "none"],
    "time_window_kind": [
        "single_day",
        "single_hour",
        "rolling_days",
        "calendar_week",
        "calendar_month",
        "explicit_range",
        "none",
    ],
    "archive_count_scope": ["whole_archive", "local_selection", "none"],
    "count_metric": [
        "all_stats",
        "knowledge_objects",
        "raw_objects",
        "files",
        "entities",
        "relations",
        "none",
    ],
    "required_tool": [
        "what_happened",
        "upcoming",
        "kg_stats",
        "web_research",
        "remind",
        "collect_files",
        "memory_search",
        "user_activity",
        "user_knowledge_search",
        "message_search",
        "list_tags",
        "none",
    ],
    "route": [
        "forced_time_prefetch",
        "forced_archive_stats",
        "external_freshness",
        "reminder_action",
        "file_collection",
        "ordinary_archive_search",
        "material_intake",
        "structural_correction",
        "general_conversation",
        "person_activity",
        "own_message_search",
        "general_reasoning",
        "attachment_exact",
        "archive_filtered_search",
    ],
}

EXPECTED_ACCEPTANCE = {
    "expected_case_counts": {
        "time_positive": 25,
        "time_positive_past": 15,
        "time_positive_future": 10,
        "time_control": 20,
        "global_count_positive": 20,
        "local_count_control": 20,
        "total": 85,
    },
    "questions_must_be_unique": True,
    "time_positives_require_exactly_one_expected_tool": True,
    "time_controls_forbid": ["what_happened", "upcoming"],
    "global_count_positives_require_exactly_one_expected_tool": True,
    "local_count_controls_forbid": ["kg_stats"],
}


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _cases(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *fixture["time_positives"],
        *fixture["time_controls"],
        *fixture["global_count_positives"],
        *fixture["local_count_controls"],
    ]


def test_package_b_fixture_bytes_are_frozen_before_implementation() -> None:
    assert hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() == FIXTURE_SHA256


def test_package_b_fixture_is_declared_synthetic_and_has_no_hidden_payload_section() -> None:
    fixture = _fixture()

    assert set(fixture) == {
        "$schema",
        "schema_version",
        "synthetic_only",
        "reference_clock",
        "purpose",
        "privacy",
        "closed_enums",
        "acceptance",
        "time_positives",
        "time_controls",
        "global_count_positives",
        "local_count_controls",
    }
    assert fixture["$schema"] == "friday.package_b_routing_holdout.v1"
    assert fixture["schema_version"] == 1
    assert fixture["synthetic_only"] is True
    assert fixture["reference_clock"] == {
        "date": "2026-08-08",
        "timezone": "Europe/Moscow",
    }
    assert fixture["privacy"] == {
        "contains_live_chat": False,
        "contains_live_file_text": False,
        "contains_real_people": False,
        "contains_real_tenant_ids": False,
        "contains_model_responses": False,
    }


def test_package_b_preregistered_enums_and_acceptance_do_not_drift() -> None:
    fixture = _fixture()

    assert fixture["closed_enums"] == EXPECTED_ENUMS
    assert fixture["acceptance"] == EXPECTED_ACCEPTANCE


def test_package_b_has_exactly_85_unique_prompts_and_ids() -> None:
    fixture = _fixture()
    cases = _cases(fixture)
    questions = [str(case["question"]) for case in cases]
    normalized_questions = [" ".join(question.casefold().split()) for question in questions]
    ids = [str(case["id"]) for case in cases]

    assert len(cases) == 85
    assert len(set(questions)) == 85
    assert len(set(normalized_questions)) == 85
    assert len(set(ids)) == 85
    assert all(question.strip() for question in questions)


def test_package_b_case_categories_counts_and_ids_are_preregistered() -> None:
    fixture = _fixture()
    time_positives = fixture["time_positives"]
    time_controls = fixture["time_controls"]
    global_positives = fixture["global_count_positives"]
    local_controls = fixture["local_count_controls"]

    assert len(time_positives) == 25
    assert len(time_controls) == 20
    assert len(global_positives) == 20
    assert len(local_controls) == 20

    assert [case["id"] for case in time_positives[:15]] == [f"k14_past_{index:03d}" for index in range(1, 16)]
    assert [case["id"] for case in time_positives[15:]] == [
        f"k14_future_{index:03d}" for index in range(1, 11)
    ]
    assert [case["id"] for case in time_controls] == [f"k14_control_{index:03d}" for index in range(1, 21)]
    assert [case["id"] for case in global_positives] == [f"k02_global_{index:03d}" for index in range(1, 21)]
    assert [case["id"] for case in local_controls] == [f"k02_control_{index:03d}" for index in range(1, 21)]

    assert {case["group"] for case in time_positives} == {"time_positive"}
    assert {case["group"] for case in time_controls} == {"time_control"}
    assert {case["group"] for case in global_positives} == {"global_count_positive"}
    assert {case["group"] for case in local_controls} == {"local_count_control"}


def test_package_b_expected_labels_stay_inside_the_closed_enums() -> None:
    fixture = _fixture()

    for case in _cases(fixture):
        assert set(case) == {"id", "group", "question", "expected"}
        expected = case["expected"]
        assert set(expected) in (
            {
                "time_direction",
                "time_window_kind",
                "archive_count_scope",
                "count_metric",
                "required_tool",
                "route",
            },
            {
                "time_direction",
                "time_window_kind",
                "archive_count_scope",
                "count_metric",
                "required_tool",
                "route",
                "forbidden_tools",
            },
        )
        assert case["group"] in EXPECTED_ENUMS["case_group"]
        for field in (
            "time_direction",
            "time_window_kind",
            "archive_count_scope",
            "count_metric",
            "required_tool",
            "route",
        ):
            assert expected[field] in EXPECTED_ENUMS[field]
        assert set(expected.get("forbidden_tools", ())) <= set(EXPECTED_ENUMS["required_tool"])


def test_package_b_positive_and_control_tool_boundaries_are_fixed() -> None:
    fixture = _fixture()
    time_positives = fixture["time_positives"]

    assert [case["expected"]["time_direction"] for case in time_positives].count("past") == 15
    assert [case["expected"]["time_direction"] for case in time_positives].count("future") == 10
    for case in time_positives:
        expected = case["expected"]
        required = "what_happened" if expected["time_direction"] == "past" else "upcoming"
        assert expected["required_tool"] == required
        assert expected["route"] == "forced_time_prefetch"
        assert expected["archive_count_scope"] == "none"

    for case in fixture["time_controls"]:
        assert case["expected"]["forbidden_tools"] == ["what_happened", "upcoming"]

    for case in fixture["global_count_positives"]:
        expected = case["expected"]
        assert expected["archive_count_scope"] == "whole_archive"
        assert expected["required_tool"] == "kg_stats"
        assert expected["route"] == "forced_archive_stats"

    for case in fixture["local_count_controls"]:
        expected = case["expected"]
        assert expected["archive_count_scope"] != "whole_archive"
        assert expected["forbidden_tools"] == ["kg_stats"]
