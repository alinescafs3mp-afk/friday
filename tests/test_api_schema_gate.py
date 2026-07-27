"""The API schema is a map of the deployment, and it needs a capability.

`/api/docs` and `/api/openapi.json` required authentication but no capability.
The guest preset — created automatically for anyone who writes in an allow-listed
GROUP chat — could therefore read the whole inventory of routes, path and query
parameters and admin endpoints, including every one its own preset is forbidden
to call. Authentication says who you are; it does not say what you may see.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from jericho.permissions import DEFAULT_PRESET_KEY
from jericho.server import create_app


def _guest_token(client) -> str:
    """A real low-privilege account, created the way the product creates them."""
    import hashlib

    storage = client.app.state.storage
    storage.ensure_user("guest-user", source="api-token", preset_key=DEFAULT_PRESET_KEY)
    storage.update_user("guest-user", preset_key=DEFAULT_PRESET_KEY)
    token = "g" * 48
    storage.create_api_token("guest-user", hashlib.sha256(token.encode()).hexdigest(), label="test-guest")
    return token


def test_the_owner_can_read_the_schema(settings):
    with TestClient(create_app(settings)) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        schema = client.get("/api/openapi.json", headers=owner)
        assert schema.status_code == 200, schema.text
        assert "/api/knowledge" in str(schema.json()["paths"])
        assert client.get("/api/docs", headers=owner).status_code == 200


def test_a_guest_cannot_enumerate_the_api(settings):
    with TestClient(create_app(settings)) as client:
        token = _guest_token(client)
        guest = {"Authorization": f"Bearer {token}"}
        # The premise: this token really is a working, lower-privilege account.
        assert client.get("/api/me", headers=guest).status_code == 200

        for path in ("/api/openapi.json", "/api/docs"):
            response = client.get(path, headers=guest)
            assert response.status_code == 403, f"{path} -> {response.status_code}"


def test_the_schema_still_needs_credentials(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/openapi.json").status_code == 401
