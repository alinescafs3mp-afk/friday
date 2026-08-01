"""A correlation id a client chose must not be able to forge evidence.

`X-Request-ID` is written into every audit row for the request and echoed back in
the response. It used to be taken verbatim, so a caller could stamp their own
writes with somebody else's id — colliding their actions with a victim's in the
durable audit trail — or with kilobytes of junk, in the exact field an
investigator uses to tie events together.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from friday.server import create_app


def test_a_well_formed_id_is_honoured(settings):
    with TestClient(create_app(settings)) as client:
        owner = {
            "Authorization": f"Bearer {settings.api_token}",
            "X-Request-ID": "7f3c9a1e-2b4d-4f60-9d3a-1c2e5b7a8d90",
        }
        response = client.get("/api/me", headers=owner)
        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == "7f3c9a1e-2b4d-4f60-9d3a-1c2e5b7a8d90"


def test_a_hostile_id_is_replaced_not_reflected(settings):
    hostile = [
        "a" * 500,  # unbounded padding
        "id with spaces",
        "line\r\nInjected: header",  # header splitting
        "<script>alert(1)</script>",
        "",
    ]
    with TestClient(create_app(settings)) as client:
        for value in hostile:
            response = client.get(
                "/api/me",
                headers={"Authorization": f"Bearer {settings.api_token}", "X-Request-ID": value},
            )
            assert response.status_code == 200, value
            returned = response.headers["X-Request-ID"]
            assert returned != value, f"reflected verbatim: {value!r}"
            assert len(returned) == 24 and returned.isalnum(), returned


def test_the_audit_trail_records_the_server_id(settings):
    """What lands in the audit row is what the response reports — and it is ours."""
    with TestClient(create_app(settings)) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        response = client.post(
            "/api/kg/entities",
            json={"name": "Атлас", "entity_type": "project"},
            headers={**owner, "X-Request-ID": "spoofed id with spaces"},
        )
        assert response.status_code == 200, response.text
        generated = response.headers["X-Request-ID"]

        storage = client.app.state.storage
        rows = storage.execute("SELECT request_id FROM audit_log ORDER BY created_at DESC LIMIT 5").fetchall()
        recorded = {str(row["request_id"] or "") for row in rows}
        assert "spoofed id with spaces" not in recorded
        assert generated in recorded
