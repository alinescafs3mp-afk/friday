"""The HTTP surface is a contract; moving code must not move the contract.

`create_app` grew into a 1295-line function with 51 routes declared as nested
closures, which is what makes splitting it risky: a route that silently fails to
mount, changes method, or loses its path breaks clients, and no unit test aimed at a
*different* endpoint would notice.

The surface is read from the OpenAPI schema, NOT from ``app.routes``: on FastAPI
0.139 routes added via ``include_router`` do not appear in ``app.routes`` at all, so
introspecting that attribute silently misses every router-mounted endpoint — 67 of
them here, including the whole admin API. The schema is what clients actually see.

The count is asserted rather than the full list: the list lives in the schema, and a
second copy of 134 operation strings would rot. What must never happen silently is an
endpoint appearing or vanishing.
"""

from __future__ import annotations

from jericho.server import create_app

# Bumped deliberately when an endpoint is added or removed, never to make a test pass.
EXPECTED_OPERATIONS = 134
# Areas that are mounted through include_router, i.e. exactly the ones app.routes
# cannot see. Pinning their sizes catches a router that quietly stops being included.
EXPECTED_BY_PREFIX = {
    "/api/admin": 70,
    "/api/kg": 14,
    "/api/missions": 4,
}


def _surface(settings) -> set[str]:
    schema = create_app(settings).openapi()
    return {
        f"{method.upper()} {path}" for path, operations in schema["paths"].items() for method in operations
    }


def test_http_contract_size_is_pinned(settings):
    surface = _surface(settings)
    assert len(surface) == EXPECTED_OPERATIONS, (
        f"the HTTP surface changed: {len(surface)} operations, expected {EXPECTED_OPERATIONS}. "
        "Update EXPECTED_OPERATIONS only when an endpoint was added or removed on purpose."
    )


def test_router_mounted_areas_are_all_present(settings):
    """These are invisible to app.routes, so nothing else would notice them vanishing."""
    surface = _surface(settings)
    for prefix, expected in EXPECTED_BY_PREFIX.items():
        paths = {item.split(" ", 1)[1] for item in surface if item.split(" ", 1)[1].startswith(prefix)}
        assert len(paths) == expected, f"{prefix}: {len(paths)} paths, expected {expected}"


def test_extracted_kg_router_is_reachable(settings):
    """The knowledge-graph routes were lifted out of create_app into their own module;
    they must still answer on the same paths."""
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        assert client.get("/api/kg/stats", headers=headers).status_code == 200
        assert client.get("/api/kg/entities", headers=headers).status_code == 200
