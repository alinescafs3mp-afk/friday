import pytest

from friday.orchestration.coding_worker_limits import (
    MAX_CPU_SEC,
    MAX_MEMORY_BYTES,
    MAX_WALL_CLOCK_SEC,
    CodingWorkerLimitsFactsV1,
    CodingWorkerLimitsReason,
    CodingWorkerLimitsState,
    build_coding_worker_limits,
)


def test_empty_facts_are_empty() -> None:
    result = build_coding_worker_limits("limits:1", "turn:1")

    assert result.limits is CodingWorkerLimitsState.EMPTY
    assert result.wall_clock_sec is None


def test_positive_limits_at_closed_maxima_are_bounded() -> None:
    result = build_coding_worker_limits(
        "limits:1",
        "turn:1",
        CodingWorkerLimitsFactsV1(MAX_WALL_CLOCK_SEC, MAX_MEMORY_BYTES, MAX_CPU_SEC),
    )

    assert result.limits is CodingWorkerLimitsState.BOUNDED
    assert result.wall_clock_sec == MAX_WALL_CLOCK_SEC
    assert result.memory_bytes == MAX_MEMORY_BYTES
    assert result.cpu_sec == MAX_CPU_SEC


@pytest.mark.parametrize(
    "facts",
    (
        {"wall_clock_sec": 0, "memory_bytes": 100, "cpu_sec": 1},
        {"wall_clock_sec": 1, "memory_bytes": 0, "cpu_sec": 1},
        {"wall_clock_sec": 1, "memory_bytes": 100, "cpu_sec": 0},
        {"wall_clock_sec": MAX_WALL_CLOCK_SEC + 1, "memory_bytes": 100, "cpu_sec": 1},
        {"wall_clock_sec": 1, "memory_bytes": MAX_MEMORY_BYTES + 1, "cpu_sec": 1},
        {"wall_clock_sec": 1, "memory_bytes": 100, "cpu_sec": MAX_CPU_SEC + 1},
        {"wall_clock_sec": True, "memory_bytes": 100, "cpu_sec": 1},
    ),
)
def test_invalid_or_out_of_bound_limits_block_without_values(facts: dict[str, object]) -> None:
    result = build_coding_worker_limits("limits:1", "turn:1", facts)

    assert result.limits is CodingWorkerLimitsState.BLOCKED
    assert result.wall_clock_sec is None
    assert result.memory_bytes is None
    assert result.cpu_sec is None


@pytest.mark.parametrize(
    "facts",
    (
        {"wall_clock_sec": 1, "memory_bytes": 100},
        {"wall_clock_sec": 1, "cpu_sec": 1},
        {"memory_bytes": 100, "cpu_sec": 1},
    ),
)
def test_missing_limit_is_blocked(facts: dict[str, object]) -> None:
    result = build_coding_worker_limits("limits:1", "turn:1", facts)

    assert result.limits is CodingWorkerLimitsState.BLOCKED
    assert result.reason in {
        CodingWorkerLimitsReason.MISSING_WALL_CLOCK,
        CodingWorkerLimitsReason.MISSING_MEMORY,
        CodingWorkerLimitsReason.MISSING_CPU,
    }


def test_unknown_fields_fail_closed_and_result_is_frozen() -> None:
    result = build_coding_worker_limits(
        "limits:1", "turn:1", {"wall_clock_sec": 1, "memory_bytes": 100, "cpu_sec": 1, "extra": 1}
    )

    assert result.limits is CodingWorkerLimitsState.BLOCKED
    with pytest.raises(AttributeError):
        result.limits = CodingWorkerLimitsState.BOUNDED  # type: ignore[misc]
