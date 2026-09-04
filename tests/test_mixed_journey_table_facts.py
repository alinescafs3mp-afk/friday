import pytest

from friday.orchestration.mixed_journey_table_facts import (
    MAX_TABLE_DIMENSION,
    MixedJourneyTableFactsState,
    build_mixed_journey_table_facts,
)

DIGEST = "c" * 64


def test_empty_present_and_mapping_round_trip() -> None:
    assert build_mixed_journey_table_facts().state is MixedJourneyTableFactsState.EMPTY
    result = build_mixed_journey_table_facts("table-1", DIGEST, 8, 4)
    assert result.state is MixedJourneyTableFactsState.PRESENT
    assert result.row_count == 8
    assert build_mixed_journey_table_facts(result.to_mapping()) == result


@pytest.mark.parametrize(
    "facts",
    [
        {"table_id": "/home/user/table.xlsx", "sha256": DIGEST},
        {"table_id": "../table", "sha256": DIGEST},
        {"table_id": "private_table", "sha256": DIGEST},
        {"table_id": "table-1", "sha256": DIGEST, "row_count": MAX_TABLE_DIMENSION + 1},
        {"table_id": "table-1", "sha256": DIGEST, "cells": [["secret"]]},
        {"table_id": "table-1", "sha256": "wrong"},
    ],
)
def test_table_hazards_block_without_cells(facts: dict[str, object]) -> None:
    result = build_mixed_journey_table_facts(facts)
    assert result.state is MixedJourneyTableFactsState.BLOCKED
    assert result.table_id is None
    assert result.sha256 is None
    assert result.row_count is None
    assert "secret" not in str(result.to_mapping())
