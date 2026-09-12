"""Faults at actual app response boundaries; never whole-soak/live evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from tools import release_1_0_app_soak as soak


@pytest.mark.parametrize(
    ("fault", "expected_failure", "failed_operation"),
    [
        ("missing_upload_handle", "owner_upload_handle_missing", "owner_upload"),
        ("empty_owner_search", "owner_source_attribution_invalid", "owner_source_query"),
        ("wrong_source_attribution", "owner_source_attribution_invalid", "owner_source_query"),
        ("foreign_source_visible", "foreign_source_visible", "foreign_source_query"),
        ("missing_owner_artifact", "owner_artifact_read_refused", "owner_artifact_read"),
        ("foreign_artifact_visible", "foreign_artifact_not_refused", "foreign_artifact_refusal"),
    ],
)
def test_actual_app_response_fault_rejects_the_affected_operation(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    expected_failure: str,
    failed_operation: str,
) -> None:
    tuned = replace(
        settings,
        shared_archive=False,
        llm_enabled=False,
        embeddings_enabled=False,
        workers_enabled=False,
        reminders_enabled=True,
        quiet_hours_start=0,
        quiet_hours_end=0,
        telegram_allowed_chat_ids=[5001, 5002, 5003, 5004],
        telegram_owner_chat_ids=[5001],
        telegram_open_registration=False,
    )
    policy = soak.SoakPolicy.from_value(
        {
            "schema": soak.POLICY_SCHEMA,
            "run_id": hashlib.sha256(fault.encode()).hexdigest()[:32],
            "candidate_sha256": hashlib.sha256(Path(soak.__file__).read_bytes()).hexdigest(),
            "suite_revision": "app-soak-actual-api-fault-controls-166",
            "config_sha256": soak._digest(soak.safe_settings_identity(tuned)),
            "duration_sec": 0.25,
            "cadence_sec": 0.25,
            "deadline_sec": 30.0,
            "max_requests": 64,
            "request_concurrency": 1,
            "principals": [
                {"user_id": LEGACY_OWNER_USER_ID, "chat_id": 5001, "preset_key": "owner"},
                *(
                    {"user_id": f"soak-control-user-{n}", "chat_id": 5001 + n, "preset_key": "user"}
                    for n in range(1, 4)
                ),
            ],
            "resources": {
                "max_rss_bytes": 1 << 30,
                "max_fd_count": 1024,
                "max_thread_count": 128,
                "max_database_bytes": 256 << 20,
                "max_wal_bytes": 128 << 20,
                "max_evidence_bytes": 32 << 20,
            },
        }
    )
    original = TestClient.send
    observed: dict = {"mutations": 0, "source_queries": 0, "artifact_reads": 0}

    def at_response_boundary(self, request, *args, **kwargs):
        response = original(self, request, *args, **kwargs)
        response.read()
        path = request.url.path
        method = request.method.upper()
        replacement = None
        if method == "POST" and path == "/api/files":
            assert response.status_code == 200
            payload = response.json()
            assert payload.get("raw_object_id")
            if fault == "missing_upload_handle":
                replacement = {k: v for k, v in payload.items() if k != "raw_object_id"}
        elif method == "GET" and path == "/api/knowledge/sources":
            observed["source_queries"] += 1
            assert response.status_code == 200
            payload = response.json()
            if observed["source_queries"] == 1:
                assert payload["count"] == 1 and len(payload["items"]) == 1
                observed["owned_source"] = payload["items"][0]
                if fault == "empty_owner_search":
                    replacement = {**payload, "items": [], "count": 0}
                elif fault == "wrong_source_attribution":
                    replacement = {
                        **payload,
                        "items": [{**payload["items"][0], "source_ref": "wrong-source"}],
                    }
            elif observed["source_queries"] == 2:
                assert payload["count"] == 0 and payload["items"] == []
                if fault == "foreign_source_visible":
                    replacement = {**payload, "items": [observed["owned_source"]], "count": 1}
        elif method == "GET" and path.startswith("/api/files/raw_"):
            observed["artifact_reads"] += 1
            if observed["artifact_reads"] == 1:
                assert response.status_code == 200 and response.content
                observed["owned_artifact"] = response.content
                if fault == "missing_owner_artifact":
                    observed["mutations"] += 1
                    return httpx.Response(404, json={"detail": "missing"}, request=response.request)
            elif observed["artifact_reads"] == 2:
                assert response.status_code == 404
                if fault == "foreign_artifact_visible":
                    observed["mutations"] += 1
                    return httpx.Response(200, content=observed["owned_artifact"], request=response.request)
        if replacement is not None:
            observed["mutations"] += 1
            return httpx.Response(response.status_code, json=replacement, request=response.request)
        return response

    monkeypatch.setattr(TestClient, "send", at_response_boundary)
    evidence = tmp_path / "fault-evidence"
    report = soak.run_actual_app_soak(tuned, policy, evidence)

    assert observed["mutations"] == 1, {
        "reason": "The fault must hit one actual product response",
        "failure_codes": report["failure_codes"],
        "request_accounting": report["request_accounting"],
        "duration_coverage": report["duration_coverage"],
    }
    assert report["status"] == "APP_SOAK_PROFILE_INCOMPLETE"
    assert report["failure_codes"][0] == expected_failure
    errors = [json.loads(p.read_text()) for p in evidence.glob("operation-*.error.json")]
    assert len(errors) == 1
    assert errors[0]["name"] == failed_operation
    assert errors[0]["reason_code"] == expected_failure
    assert errors[0]["status"] == "FAIL"
    attempted_paths = [
        json.loads(path.read_text())["path"] for path in evidence.glob("session-*/request-*.start.json")
    ]
    assert "/api/chat" not in attempted_paths
    assert report["go_emitted"] is False
    assert report["full_gate_credit"] is False
    assert report["live_telegram_proof"] is False


def test_http_200_offline_without_reminder_effect_is_red_and_continues_only_identity_reads(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tuned = replace(
        settings,
        shared_archive=False,
        llm_enabled=False,
        embeddings_enabled=False,
        workers_enabled=False,
        reminders_enabled=True,
        quiet_hours_start=0,
        quiet_hours_end=0,
        telegram_allowed_chat_ids=[5001, 5002, 5003, 5004],
        telegram_owner_chat_ids=[5001],
        telegram_open_registration=False,
    )
    policy = soak.SoakPolicy.from_value(
        {
            "schema": soak.POLICY_SCHEMA,
            "run_id": hashlib.sha256(b"offline-no-reminder-effect").hexdigest()[:32],
            "candidate_sha256": hashlib.sha256(Path(soak.__file__).read_bytes()).hexdigest(),
            "suite_revision": "app-soak-outcome-controls-081",
            "config_sha256": soak._digest(soak.safe_settings_identity(tuned)),
            "duration_sec": 0.5,
            "cadence_sec": 0.25,
            "deadline_sec": 5.0,
            "max_requests": 64,
            "request_concurrency": 4,
            "principals": [
                {"user_id": LEGACY_OWNER_USER_ID, "chat_id": 5001, "preset_key": "owner"},
                *(
                    {"user_id": f"soak-outcome-user-{n}", "chat_id": 5001 + n, "preset_key": "user"}
                    for n in range(1, 4)
                ),
            ],
            "resources": {
                "max_rss_bytes": 1 << 30,
                "max_fd_count": 1024,
                "max_thread_count": 128,
                "max_database_bytes": 256 << 20,
                "max_wal_bytes": 128 << 20,
                "max_evidence_bytes": 32 << 20,
            },
        }
    )
    original = TestClient.send
    observed = {"chat": 0, "identity_after_chat": 0}

    def offline_boundary(self, request, *args, **kwargs):
        if request.method == "POST" and request.url.path == "/api/chat":
            observed["chat"] += 1
            return httpx.Response(
                200,
                json={"message": "Модель сейчас недоступна.", "tools_used": []},
                request=request,
            )
        if observed["chat"] and request.method == "GET" and request.url.path == "/api/me":
            observed["identity_after_chat"] += 1
        return original(self, request, *args, **kwargs)

    now = [0.0]

    def clock() -> float:
        return now[0]

    def sleeper(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(TestClient, "send", offline_boundary)
    evidence = tmp_path / "offline-red-evidence"
    report = soak.run_actual_app_soak(
        tuned,
        policy,
        evidence,
        clock=clock,
        sleeper=sleeper,
    )

    assert observed == {"chat": 1, "identity_after_chat": 4}, {
        "failure_codes": report["failure_codes"],
        "request_accounting": report["request_accounting"],
        "duration_coverage": report["duration_coverage"],
    }
    assert report["status"] == "APP_SOAK_PROFILE_FAILED"
    assert report["functional_status"] == "FAIL"
    assert report["failure_codes"] == ["reminder_creation_not_observed"]
    assert report["evidence_status"] == "EVIDENCE_COMPLETE"
    assert report["duration_coverage"]["status"] == "INDEPENDENT_READ_ONLY_AFTER_FUNCTIONAL_FAIL"
    assert report["duration_coverage"]["identity_get_rounds"] == 1
    assert report["duration_coverage"]["reminder_branch"] == "MISSING_AFTER_CREATION_FAIL"
    assert report["operation_outcomes"]["FAIL"] == 1
    assert report["operation_outcomes"]["NOT_RUN"] == 7
    assert report["go_emitted"] is False
    assert report["full_gate_credit"] is False
    assert not list(evidence.glob("destination-*.json"))
