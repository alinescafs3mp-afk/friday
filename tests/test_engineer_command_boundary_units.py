from __future__ import annotations

import pytest

from friday.organs.engineer.command import boundary
from friday.organs.engineer.command.contracts import CommandError


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("305000000", 305_000_000),
        ("305sec", 305_000_000),
        ("5min 5s", 305_000_000),
        ("1h 2min 3.5s", 3_723_500_000),
        ("1s 250ms 5usec", 1_250_005),
    ],
)
def test_parse_systemd_usec_accepts_exact_composite_spans(value: str, expected: int) -> None:
    assert boundary._parse_systemd_usec(value) == expected  # noqa: SLF001


@pytest.mark.parametrize("value", ["", "infinity", "5min garbage", "1.0000001us", "-1s"])
def test_parse_systemd_usec_rejects_unproven_spans(value: str) -> None:
    with pytest.raises(CommandError, match="resource_boundary_unproven"):
        boundary._parse_systemd_usec(value)  # noqa: SLF001
