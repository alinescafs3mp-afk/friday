"""Auth brute-force limiting and disabled-owner loopback — §19 hardening.

Failed authentication previously consumed no budget (the rate limiter ran only
after successful auth), so bearer tokens and bridge signatures could be
brute-forced without bound; and the credential-less loopback bypass granted
owner access even after the owner account was explicitly disabled.
"""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from jericho.permissions import LEGACY_OWNER_USER_ID
from jericho.server import create_app


def test_failed_auth_attempts_are_rate_limited_per_ip(settings):
    limited = replace(settings, api_auth_failure_limit_per_minute=3)
    with TestClient(create_app(limited)) as client:
        bad = {"Authorization": "Bearer " + "x" * 48}
        for _ in range(3):
            assert client.get("/api/me", headers=bad).status_code == 401

        # Budget spent: further attempts are refused before credential checks —
        # even a valid token is locked out from the abusive address.
        blocked = client.get("/api/me", headers=bad)
        assert blocked.status_code == 429
        assert blocked.headers.get("Retry-After") == "60"
        good = {"Authorization": f"Bearer {limited.api_token}"}
        assert client.get("/api/me", headers=good).status_code == 429

        # Public endpoints never consume or require the auth budget.
        assert client.get("/api/health").status_code == 200


def test_valid_auth_does_not_consume_failure_budget(settings):
    limited = replace(settings, api_auth_failure_limit_per_minute=2)
    with TestClient(create_app(limited)) as client:
        good = {"Authorization": f"Bearer {limited.api_token}"}
        for _ in range(5):
            assert client.get("/api/me", headers=good).status_code == 200
        # One failure still gets its honest 401 — successes counted nothing.
        assert client.get("/api/me", headers={"Authorization": "Bearer " + "y" * 48}).status_code == 401


@pytest.mark.asyncio
async def test_disabled_owner_cannot_use_loopback_bypass(settings):
    app = create_app(replace(settings, api_require_token_on_loopback=False))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 9000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
            first = await client.get("/api/me")
            assert first.status_code == 200
            assert first.json()["actor"]["source"] == "loopback"

            app.state.storage.update_user(LEGACY_OWNER_USER_ID, status="disabled")
            denied = await client.get("/api/me")
            assert denied.status_code == 401
            assert "disabled" in denied.json()["detail"].casefold()

            # Re-enabling restores access without any token dance.
            app.state.storage.update_user(LEGACY_OWNER_USER_ID, status="active")
            assert (await client.get("/api/me")).status_code == 200
