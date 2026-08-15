"""Focused offline regressions for the first canonical live-acceptance red."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from friday.agent_runtime import _intra_file_record_set_count, file_turn_authority

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_battery as battery  # noqa: E402


def _case(pass_id: str, index: int) -> battery.ExpandedCase:
    manifest = battery.load_manifest(battery.MANIFEST_PATHS["A"])
    return next(
        case
        for case in battery.expand_manifest_cases(manifest)
        if case.pass_id == pass_id and case.question_index == index
    )


def test_plain_two_line_composition_does_not_grant_private_file_read() -> None:
    question = _case("A-P10", 5).question

    assert _intra_file_record_set_count(question) is None
    assert file_turn_authority(question).proved("local_read") is False
    assert file_turn_authority("Верни две строки этого файла.").proved("local_read") is True
    assert file_turn_authority("Назови две строки.").proved("local_read") is True


def test_a10_plain_heading_and_one_bullet_is_a_closed_transport_shape(tmp_path: Path) -> None:
    case = _case("A-P10", 6)
    marker = battery._marker(case, "TELEGRAM")
    message = f"Короткий заголовок\n- {marker}"

    assert battery._telegram_shape_matches(case, message) is True
    state = battery._telegram_transport_probe(message, mode="normal", home=tmp_path)
    assert state["transport_delivery_marker_exact"] is True
    assert state["transport_delivery_shape_exact"] is True


@pytest.mark.parametrize(
    "message",
    [
        "Короткий заголовок\nобычная строка SYN-TELEGRAM-A10-06",
        "Заголовок\n- SYN-TELEGRAM-A10-06\n- лишний пункт",
        "Заголовок\n- SYN-TELEGRAM-A10-06\nлишний хвост",
        "<b>Заголовок</b>\n- SYN-TELEGRAM-A10-06",
    ],
)
def test_a10_plain_heading_lane_remains_exact(message: str) -> None:
    assert battery._telegram_shape_matches(_case("A-P10", 6), message) is False
