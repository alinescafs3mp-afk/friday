from __future__ import annotations

import pytest

from friday.work_budgets import size_scaled_budget_sec, stage_deadline


def test_size_scaled_budget_covers_first_mib_and_caps_large_work() -> None:
    assert (
        size_scaled_budget_sec(
            size_bytes=1,
            base_sec=45,
            seconds_per_mib=2,
            maximum_sec=180,
        )
        == 45
    )
    assert (
        size_scaled_budget_sec(
            size_bytes=(3 << 20) + 1,
            base_sec=45,
            seconds_per_mib=2,
            maximum_sec=180,
        )
        == 51
    )
    assert (
        size_scaled_budget_sec(
            size_bytes=1 << 40,
            base_sec=45,
            seconds_per_mib=2,
            maximum_sec=180,
        )
        == 180
    )


def test_stage_deadline_never_extends_parent() -> None:
    assert stage_deadline(45, now=100, parent_deadline=120) == 120
    assert stage_deadline(45, now=100, parent_deadline=200) == 145


@pytest.mark.parametrize(
    "arguments",
    [
        {"size_bytes": -1, "base_sec": 1, "seconds_per_mib": 1, "maximum_sec": 2},
        {"size_bytes": 1, "base_sec": 0, "seconds_per_mib": 1, "maximum_sec": 2},
        {"size_bytes": 1, "base_sec": 2, "seconds_per_mib": -1, "maximum_sec": 2},
        {"size_bytes": 1, "base_sec": 2, "seconds_per_mib": 1, "maximum_sec": 1},
    ],
)
def test_invalid_size_scaled_budget_is_rejected(arguments: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        size_scaled_budget_sec(**arguments)  # type: ignore[arg-type]
