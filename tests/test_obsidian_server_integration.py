from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from friday.organs import build_registry
from friday.permissions import ActorContext
from friday.server import create_app


def _enabled(settings):
    return replace(
        settings,
        obsidian_enabled=True,
        obsidian_root=settings.data_dir / "obsidian-test",
        obsidian_syncthing_binary="/bin/dash",
        obsidian_public_base_url="https://friday.example",
    )


def _route_paths(app) -> set[str]:
    paths: set[str] = set()

    def walk(routes, prefix: str = "") -> None:
        for route in routes:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                context = getattr(route, "include_context", None)
                walk(nested.routes, prefix + getattr(context, "prefix", ""))
                continue
            if (path := getattr(route, "path", None)) is not None:
                paths.add(prefix + str(path))

    walk(app.routes)
    return paths


def test_optional_organ_and_routes_exist_only_when_enabled(settings) -> None:
    disabled_names = {organ.name for organ in build_registry(settings).organs}
    disabled_paths = _route_paths(create_app(settings))
    assert "obsidian" not in disabled_names
    assert "/api/obsidian/status" not in disabled_paths
    assert "/obsidian/setup" not in disabled_paths

    enabled = _enabled(settings)
    enabled_names = {organ.name for organ in build_registry(enabled).organs}
    enabled_paths = _route_paths(create_app(enabled))
    assert "obsidian" in enabled_names
    assert {
        "/api/obsidian/status",
        "/api/obsidian/diagnostics",
        "/api/obsidian/notes/search",
        "/api/obsidian/operations",
        "/obsidian/setup",
        "/obsidian/open",
        "/api/public/obsidian/setup/resolve",
    } <= enabled_paths


def test_enabled_server_registers_tools_and_only_setup_is_public(settings) -> None:
    with TestClient(create_app(_enabled(settings))) as client:
        setup = client.get("/obsidian/setup")
        assert setup.status_code == 200
        assert setup.headers["cache-control"] == "no-store"
        assert "script-src 'self'" in setup.headers["content-security-policy"]

        assert client.get("/api/obsidian/status").status_code == 401
        assert client.get("/api/obsidian/notes").status_code == 401
        invalid = client.post(
            "/api/public/obsidian/setup/resolve",
            json={"token": "A" * 43},
        )
        assert invalid.status_code == 404

        actor = ActorContext("local-owner", "owner", "test")
        names = set(client.app.state.kernel.get_tool_names(actor))
        assert {
            "obsidian_list_vaults",
            "obsidian_list_notes",
            "obsidian_search_notes",
            "obsidian_read_note",
            "obsidian_create_note",
            "obsidian_append_note",
            "obsidian_set_properties",
            "obsidian_daily_note",
        } <= names


def test_public_setup_resolver_has_an_independent_per_ip_rate_limit(settings) -> None:
    configured = replace(_enabled(settings), obsidian_public_setup_rate_limit_per_minute=2)
    with TestClient(create_app(configured)) as client:
        first = client.post("/api/public/obsidian/setup/resolve", json={"token": "A" * 43})
        second = client.post("/api/public/obsidian/setup/resolve", json={"token": "B" * 43})
        limited = client.post("/api/public/obsidian/setup/resolve", json={"token": "C" * 43})

    assert first.status_code == 404
    assert second.status_code == 404
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
