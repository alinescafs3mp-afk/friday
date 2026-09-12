"""Deterministic additional R10 user journeys on an isolated Friday."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from tools.release_1_0_deterministic import read_gate_context
from tools.release_1_0_live_journeys import RUNNERS, run_deterministic_suite


@pytest.mark.parametrize("case_id", tuple(RUNNERS))
def test_additional_deterministic_journeys_cover_the_required_user_paths(settings, case_id, request) -> None:
    report = run_deterministic_suite(settings, [case_id], gate_context=read_gate_context(dict(os.environ)))
    for row in report["results"]:
        if row.get("evidence_dir"):
            receipt = Path(row["evidence_dir"]) / "case-receipt.json"
            request.node.user_properties.extend(
                [
                    ("r10_case_receipt", str(receipt)),
                    ("r10_case_receipt_sha256", hashlib.sha256(receipt.read_bytes()).hexdigest()),
                ]
            )
    failed = [
        row["id"] + ":" + ",".join(row.get("failure_codes") or [])
        for row in report["results"]
        if row["status"] != "PASS"
    ]
    assert report["planned"] == 1
    assert report["executed"] == 1, {
        row["id"]: {
            "failure_codes": row.get("failure_codes", []),
            "observed_safe": row.get("observed_safe", {}),
        }
        for row in report["results"]
    }
    assert report["go_emitted"] is False
    assert not failed, failed
    assert report["status"] == "PASS"
