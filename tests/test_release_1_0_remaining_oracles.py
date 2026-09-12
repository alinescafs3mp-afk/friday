"""Observed-state faults for the remaining deterministic R10 smoke cases."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from tools import release_1_0_live_journeys as journeys


@pytest.mark.parametrize("endpoint", ["files", "users"])
def test_admin_lists_enforce_access_and_exact_pagination(settings, endpoint):
    from friday.permissions import LEGACY_OWNER_USER_ID
    from friday.server import create_app

    app = create_app(settings)
    owner = journeys._owner_headers(settings)
    secret = "jrc_r10_list_reader_" + "7" * 16
    user = {"Authorization": f"Bearer {secret}"}
    route = "/api/admin/" + endpoint
    with TestClient(app) as client:
        initial_users = {row[0] for row in app.state.storage.execute("SELECT id FROM users").fetchall()}
        journeys._issue_token(app.state.storage, "r10-list-reader", "user", secret)
        app.state.storage.ensure_user("r10-list-other", preset_key="user")
        uploads = [
            journeys._upload_bytes(
                client, owner, f"r10-page-{index}.txt", f"R10_LIST_CONTENT_{index}".encode(), "text/plain"
            )
            for index in range(2)
        ]
        assert all(item["status_code"] == 200 for item in uploads)
        expected_ids = (
            {item["body"]["raw_object_id"] for item in uploads}
            if endpoint == "files"
            else initial_users | {"r10-list-reader", "r10-list-other"}
        )
        params = {"user_id": LEGACY_OWNER_USER_ID} if endpoint == "files" else {}
        for headers, status in (({}, 401), (user, 403)):
            denied = client.get(route, headers=headers, params=params)
            assert denied.status_code == status
            assert not any(str(identifier) in denied.text for identifier in expected_ids)
            assert "R10_LIST_CONTENT_" not in denied.text
        observed_ids = []
        for offset in range(len(expected_ids) + 1):
            response = client.get(route, headers=owner, params={**params, "limit": 1, "offset": offset})
            assert response.status_code == 200
            body = response.json()
            count = int(offset < len(expected_ids))
            assert type(body["count"]) is int and body["count"] == count
            assert type(body["total"]) is int and body["total"] == len(expected_ids)
            assert type(body["limit"]) is int and body["limit"] == 1
            assert type(body["offset"]) is int and body["offset"] == offset
            assert isinstance(body["items"], list) and len(body["items"]) == count
            observed_ids.extend(item["id"] for item in body["items"])
        assert len(observed_ids) == len(set(observed_ids)) and set(observed_ids) == expected_ids
        for invalid in ({"limit": 0}, {"offset": -1}):
            assert client.get(route, headers=owner, params={**params, **invalid}).status_code == 422


@pytest.mark.parametrize("runner,field", [(journeys.run_j07, "obsidian"), (journeys.run_j10, "secondary")])
def test_health_failure_cannot_copy_raw_payload_into_public_evidence(settings, monkeypatch, runner, field):
    original = TestClient.get
    canary = "SYNTHETIC-PRIVATE-HEALTH-DETAIL"

    def damaged(self, url, *args, **kwargs):
        response = original(self, url, *args, **kwargs)
        if url == "/api/health":
            body = response.json()
            body[field]["mode"] = canary
            return httpx.Response(200, json=body)
        return response

    monkeypatch.setattr(TestClient, "get", damaged)
    result = runner(settings)
    assert result["status"] == "FAIL"
    assert canary not in json.dumps(result)


def test_j02_rejects_unavailable_guest_listing(settings, monkeypatch):
    original = TestClient.get
    listings = 0

    def damaged(self, url, *args, **kwargs):
        nonlocal listings
        if url == "/api/files":
            listings += 1
            if listings == 2:
                return httpx.Response(503, json={"items": [], "count": 0})
        return original(self, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "get", damaged)
    result = journeys.run_j02(settings)
    assert result["status"] == "FAIL", result
    assert "guest_file_listing_unavailable" in result["failure_codes"]


def test_j02_binds_owner_listing_to_returned_handle(settings, monkeypatch):
    original = TestClient.get
    listings = 0

    def damaged(self, url, *args, **kwargs):
        nonlocal listings
        if url == "/api/files":
            listings += 1
            if listings == 1:
                return httpx.Response(200, json={"items": [{"id": "raw_wrong_owner"}], "count": 1})
        return original(self, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "get", damaged)
    result = journeys.run_j02(settings)
    assert result["status"] == "FAIL", result
    assert "owner_handle_missing_from_owner_listing" in result["failure_codes"]


def test_j04_rejects_unavailable_post_refusal_inventory(settings, monkeypatch):
    original = TestClient.get
    listings = 0

    def damaged(self, url, *args, **kwargs):
        nonlocal listings
        if url == "/api/knowledge":
            listings += 1
            if listings == 2:
                return httpx.Response(503, json={"items": [], "count": 0})
        return original(self, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "get", damaged)
    result = journeys.run_j04(settings)
    assert result["status"] == "FAIL", result
    assert "knowledge_listing_after_unavailable" in result["failure_codes"]


def test_j04_rejects_same_count_artifact_mutation(settings, monkeypatch):
    original = TestClient.post

    def damaged(self, url, *args, **kwargs):
        response = original(self, url, *args, **kwargs)
        if url == "/api/ingest/url":
            marker = self.app.state.settings.files_dir / "unexpected-private-url-artifact"
            marker.write_bytes(b"persistent effect without a knowledge-count change")
        return response

    monkeypatch.setattr(TestClient, "post", damaged)
    result = journeys.run_j04(settings)
    assert result["status"] == "FAIL", result
    assert "private_url_persistent_effect" in result["failure_codes"]


def test_j07_rejects_enabled_obsidian_health(settings, monkeypatch):
    original = TestClient.get

    def damaged(self, url, *args, **kwargs):
        if url == "/api/health":
            return httpx.Response(200, json={"status": "ok", "obsidian": {"mode": "enabled"}})
        return original(self, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "get", damaged)
    result = journeys.run_j07(settings)
    assert result["status"] == "FAIL", result
    assert "obsidian_health_not_exactly_disabled" in result["failure_codes"]


def test_j09_rejects_swapped_tenant_listings(settings, monkeypatch):
    original = TestClient.get
    auth_a = "Bearer jrc_r10_user_a_11111111"
    auth_b = "Bearer jrc_r10_user_b_22222222"

    def damaged(self, url, *args, **kwargs):
        if url == "/api/files":
            headers = dict(kwargs.get("headers") or {})
            if headers.get("Authorization") == auth_a:
                headers["Authorization"] = auth_b
            elif headers.get("Authorization") == auth_b:
                headers["Authorization"] = auth_a
            kwargs["headers"] = headers
        return original(self, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "get", damaged)
    result = journeys.run_j09(settings)
    assert result["status"] == "FAIL", result
    assert "principal_a_own_handle_missing" in result["failure_codes"]
    assert "principal_b_own_handle_missing" in result["failure_codes"]


def test_j09_requires_both_listing_statuses(settings, monkeypatch):
    original = TestClient.get
    listings = 0

    def damaged(self, url, *args, **kwargs):
        nonlocal listings
        if url == "/api/files":
            listings += 1
            if listings == 2:
                return httpx.Response(503, json={"items": [{"id": "raw_bogus"}], "count": 1})
        return original(self, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "get", damaged)
    result = journeys.run_j09(settings)
    assert result["status"] == "FAIL", result
    assert "principal_b_listing_unavailable" in result["failure_codes"]


def test_j10_rejects_enabled_but_unavailable_secondary_health(settings, monkeypatch):
    original = TestClient.get

    def damaged(self, url, *args, **kwargs):
        if url == "/api/health":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "secondary": {
                        "schema": "friday.optional-secondary-health.v1",
                        "role": "optional_advisory",
                        "enabled": True,
                        "configured": True,
                        "mode": "shadow",
                        "state": "healthy",
                        "available": False,
                    },
                },
            )
        return original(self, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "get", damaged)
    result = journeys.run_j10(settings)
    assert result["status"] == "FAIL", result
    assert "secondary_health_not_exactly_disabled" in result["failure_codes"]


def test_j10_requires_disabled_secondary_settings(settings):
    enabled = replace(settings, secondary_llm_enabled=True, secondary_llm_mode="shadow")
    result = journeys.run_j10(enabled)
    assert result["status"] == "FAIL", result
    assert "secondary_setting_not_disabled" in result["failure_codes"]
    assert "secondary_mode_not_disabled" in result["failure_codes"]
