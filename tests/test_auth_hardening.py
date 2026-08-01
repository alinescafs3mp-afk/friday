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

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app


def test_failed_auth_attempts_are_rate_limited_per_ip(settings):
    """The budget is spent by failures and it gates failures — not the caller.

    It used to gate the ADDRESS: once spent, credentials were not evaluated at
    all and a valid token got 429 like everything else. That reads as strict, and
    on this deployment it is a denial of service. Everything arrives from
    127.0.0.1 — the admin UI, the CLI, the Telegram bridge, the owner's browser —
    so ten credential-less requests took the whole API offline for a minute, for
    everyone. Any web page can send exactly those (`fetch(url, {mode:'no-cors'})`
    is a simple GET: no preflight, opaque response, nothing to consent to).

    Brute force is unaffected: guessing means failing, failures still spend the
    budget, and once it is spent every failure is answered 429 instead of 401.
    """
    limited = replace(settings, api_auth_failure_limit_per_minute=3)
    with TestClient(create_app(limited)) as client:
        bad = {"Authorization": "Bearer " + "x" * 48}
        for _ in range(3):
            assert client.get("/api/me", headers=bad).status_code == 401

        # Budget spent: further FAILURES are refused with 429, not 401.
        blocked = client.get("/api/me", headers=bad)
        assert blocked.status_code == 429
        assert blocked.headers.get("Retry-After") == "60"
        # …and the owner still gets in with a valid token.
        good = {"Authorization": f"Bearer {limited.api_token}"}
        assert client.get("/api/me", headers=good).status_code == 200

        # Public endpoints never consume or require the auth budget.
        assert client.get("/api/health").status_code == 200


def test_anonymous_traffic_cannot_lock_the_owner_out(settings):
    """The drive-by shape: no credentials at all, then the owner tries to work."""
    limited = replace(settings, api_auth_failure_limit_per_minute=3)
    with TestClient(create_app(limited)) as client:
        for _ in range(10):
            assert client.get("/api/me").status_code in {401, 429}
        good = {"Authorization": f"Bearer {limited.api_token}"}
        assert client.get("/api/me", headers=good).status_code == 200


def test_valid_auth_does_not_consume_failure_budget(settings):
    limited = replace(settings, api_auth_failure_limit_per_minute=2)
    with TestClient(create_app(limited)) as client:
        good = {"Authorization": f"Bearer {limited.api_token}"}
        for _ in range(5):
            assert client.get("/api/me", headers=good).status_code == 200
        # One failure still gets its honest 401 — successes counted nothing.
        assert client.get("/api/me", headers={"Authorization": "Bearer " + "y" * 48}).status_code == 401


def test_a_route_level_valueerror_is_not_charged_to_the_auth_budget(settings):
    """A bug in a handler must not read as a credential attack on the owner.

    The middleware's ``except ValueError`` was outside ``call_next``, so *any*
    ValueError escaping a route handler was answered 401 "malformed credentials"
    and billed to the per-IP auth-failure budget. Two consequences, both bad: the
    caller is told their credentials are wrong when the real fault is a 500, and
    a handful of such requests spend the budget and lock the legitimate owner out
    of their own instance with 429 — self-inflicted denial of service from a bug
    that has nothing to do with authentication.
    """
    limited = replace(settings, api_auth_failure_limit_per_minute=2)
    app = create_app(limited)

    async def boom():
        raise ValueError("mode must be dialogue, knowledge_work, or research")

    app.add_api_route("/api/_boom", boom, methods=["GET"], include_in_schema=False)

    good = {"Authorization": f"Bearer {limited.api_token}"}
    with TestClient(app, raise_server_exceptions=False) as client:
        for _ in range(4):  # comfortably past the 2-failure budget
            assert client.get("/api/_boom", headers=good).status_code == 500
        # The budget is untouched, so the owner is still served.
        assert client.get("/api/me", headers=good).status_code == 200
        # …and a real credential failure still consumes it as before.
        bad = {"Authorization": "Bearer " + "x" * 48}
        assert client.get("/api/me", headers=bad).status_code == 401
        assert client.get("/api/me", headers=bad).status_code == 401
        assert client.get("/api/me", headers=bad).status_code == 429


def test_malformed_credentials_still_cost_the_budget(settings):
    """The narrowed catch must keep charging genuinely malformed credentials.

    A bridge signature with a non-numeric timestamp raises ValueError inside
    ``_authenticate`` — that one is an authentication failure and has to stay one,
    or the brute-force budget can be bypassed by sending garbage instead of a
    wrong-but-well-formed token.
    """
    limited = replace(settings, api_auth_failure_limit_per_minute=2)
    with TestClient(create_app(limited)) as client:
        garbage = {
            "X-Friday-Timestamp": "not-a-number",
            "X-Friday-User": "42",
            "X-Friday-Chat": "42",
            "X-Friday-Signature": "deadbeef",
        }
        assert client.get("/api/me", headers=garbage).status_code == 401
        assert client.get("/api/me", headers=garbage).status_code == 401
        assert client.get("/api/me", headers=garbage).status_code == 429


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
