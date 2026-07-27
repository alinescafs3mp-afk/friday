"""Admin hardening regressions: strict CSP and the loopback CSRF guard.

Compliance §8 closed two gaps: the admin UI required ``'unsafe-inline'`` in the
CSP, and the credential-less loopback owner bypass accepted any browser-shaped
request, leaving mutations open to cross-site request forgery and DNS
rebinding. These tests pin the strict policy and the guard's decision table.
"""

from __future__ import annotations

import re
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from jericho.admin_ui import STATIC_DIR

DETECT = "/api/kg/resolutions/detect"


def _loopback_client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 9000))
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000")


def test_csp_is_strict_without_unsafe_inline(settings):
    from jericho.server import create_app

    client = TestClient(create_app(settings))
    for path in ("/admin/", "/api/health"):
        csp = client.get(path).headers["Content-Security-Policy"]
        assert "unsafe-inline" not in csp
        assert "script-src 'self'" in csp
        assert "style-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "object-src 'none'" in csp


def test_admin_ui_ships_no_inline_script_styles_or_handlers():
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert (STATIC_DIR / "app.css").is_file()

    # The page must load only external, same-origin assets.
    assert 'src="./app.js"' in index
    assert 'href="./app.css"' in index
    # No <script> with an inline body and no <style> block at all.
    for tag in re.findall(r"<script\b[^>]*>", index):
        assert "src=" in tag, f"inline script tag: {tag}"
    assert "<style" not in index

    # Neither the skeleton nor any innerHTML template may (re)introduce inline
    # handlers or style attributes — the strict CSP blocks them silently.
    for name, source in (("index.html", index), ("app.js", app_js)):
        for marker in ("onclick=", 'onchange="', "onsubmit=", 'style="', "javascript:"):
            assert marker not in source, f"{name} contains {marker}"


@pytest.mark.asyncio
async def test_loopback_guard_blocks_cross_origin_browser_mutations(settings):
    from jericho.server import create_app

    app = create_app(replace(settings, api_require_token_on_loopback=False))
    async with app.router.lifespan_context(app), _loopback_client(app) as client:
        # Non-browser client (no Origin/Sec-Fetch-Site): bypass still works.
        plain = await client.post(DETECT)
        assert plain.status_code == 200

        # Same-origin admin UI fetch: allowed.
        same = await client.post(DETECT, headers={"Origin": "http://127.0.0.1:8000"})
        assert same.status_code == 200

        # Local frontend on another loopback port: allowed.
        local = await client.post(DETECT, headers={"Origin": "http://localhost:3000"})
        assert local.status_code == 200

        # Cross-site page steering the browser into localhost: refused.
        forged = await client.post(DETECT, headers={"Origin": "https://evil.example"})
        assert forged.status_code == 403
        assert "loopback" in forged.json()["detail"].casefold()

        # Opaque/sandboxed origin is not a local client either.
        null_origin = await client.post(DETECT, headers={"Origin": "null"})
        assert null_origin.status_code == 403

        # Browser metadata alone is enough to refuse cross-site senders.
        fetch_site = await client.post(DETECT, headers={"Sec-Fetch-Site": "cross-site"})
        assert fetch_site.status_code == 403
        same_site = await client.post(DETECT, headers={"Sec-Fetch-Site": "same-origin"})
        assert same_site.status_code == 200

        # Reads are guarded on the same terms as writes. They used to be exempt,
        # reasoning that "cross-origin reads stay blocked by CORS" — but CORS
        # blocks reading the RESPONSE, not sending the request, and an
        # owner-authority GET has consequences of its own: `?synthesize=true`
        # runs the model over personal data, file routes emit bytes and write
        # audit rows, and every such request spends the owner's rate budget.
        read = await client.get("/api/me")  # non-browser client: still fine
        assert read.status_code == 200
        assert read.json()["actor"]["source"] == "loopback"

        same_origin_read = await client.get("/api/me", headers={"Sec-Fetch-Site": "same-origin"})
        assert same_origin_read.status_code == 200  # the admin UI's own fetches
        typed_url = await client.get("/api/me", headers={"Sec-Fetch-Site": "none"})
        assert typed_url.status_code == 200  # the owner typing the address

        drive_by = await client.get("/api/me", headers={"Sec-Fetch-Site": "cross-site"})
        assert drive_by.status_code == 403
        foreign_origin = await client.get("/api/me", headers={"Origin": "https://evil.example"})
        assert foreign_origin.status_code == 403


@pytest.mark.asyncio
async def test_loopback_guard_blocks_dns_rebinding_host(settings):
    from jericho.server import create_app

    app = create_app(replace(settings, api_require_token_on_loopback=False))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 9000))
        # A DNS-rebound page reaches 127.0.0.1 while keeping its foreign Host.
        async with httpx.AsyncClient(transport=transport, base_url="http://rebound.evil.example") as client:
            response = await client.get("/api/me")
            assert response.status_code == 403
            assert "host" in response.json()["detail"].casefold()


def test_bearer_token_auth_ignores_origin(settings):
    """Explicit credentials are not CSRF-able; foreign Origins stay allowed."""
    from jericho.server import create_app

    with TestClient(create_app(settings)) as client:
        response = client.post(
            DETECT,
            headers={
                "Authorization": f"Bearer {settings.api_token}",
                "Origin": "https://third-party.example",
            },
        )
        assert response.status_code == 200


def test_auth_failure_is_audited_without_leaking_the_secret(settings):
    """A rejected credential leaves a durable, secret-free forensic record."""
    from jericho.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        rejected = client.get(
            "/api/admin/overview",
            headers={"Authorization": "Bearer wrong-token-9f8e7d6c5b4a"},
        )
        assert rejected.status_code == 401

        row = app.state.storage.execute(
            "SELECT user_id, action, target_id, ip_address, after_json "
            "FROM audit_log WHERE action='auth.failed' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["target_id"] == "invalid_credentials"
        assert row["user_id"] == "anonymous"
        assert row["ip_address"]  # the IP is the key forensic datum
        # The attempted secret is never persisted.
        assert "wrong-token" not in (row["after_json"] or "")
