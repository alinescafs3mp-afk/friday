"""Deterministic additional R10 user journeys on an isolated Friday."""

from __future__ import annotations

from tools.release_1_0_live_journeys import RUNNERS, run_deterministic_suite


def test_additional_deterministic_journeys_cover_the_required_user_paths(settings) -> None:
    report = run_deterministic_suite(settings)
    failed = [
        row["id"] + ":" + ",".join(row.get("failure_codes") or [])
        for row in report["results"]
        if row["status"] != "PASS"
    ]
    assert report["planned"] == len(RUNNERS)
    assert report["executed"] == len(RUNNERS)
    assert report["go_emitted"] is False
    assert not failed, failed
    assert report["status"] == "PASS"
