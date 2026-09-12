"""Discovery must observe source, schema and registered runtime entrances."""

from __future__ import annotations

import pytest

from tools import release_1_0_acceptance as acceptance


@pytest.mark.parametrize("fault", ["hidden", "mounted", "source_only", "included", "included_mount"])
def test_runtime_and_source_only_entrances_cannot_disappear_from_audit(settings, monkeypatch, fault):
    from fastapi import APIRouter
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from friday import server

    original_create = server.create_app
    original_source = acceptance.discover_api_from_source

    async def answer(_request):
        return JSONResponse({"synthetic": True})

    expected = {
        "hidden": {"api:GET /api/r10-hidden-probe"},
        "mounted": {"api:MOUNT /r10-mounted", "api:GET /r10-mounted/private"},
        "source_only": {"api:GET /api/r10-source-only-probe"},
        "included": {"api:GET /dynamic/nested/hidden"},
        "included_mount": {"api:MOUNT /dynamic/nested/attached", "api:GET /dynamic/nested/attached/private"},
    }[fault]

    def create(settings_override=None):
        app = original_create(settings_override)
        if fault == "hidden":
            app.add_api_route("/api/r10-hidden-probe", answer, methods=["GET"], include_in_schema=False)
        elif fault == "mounted":
            app.mount("/r10-mounted", Starlette(routes=[Route("/private", answer)]))
        elif fault in {"included", "included_mount"}:
            leaf = APIRouter()
            if fault == "included":
                leaf.add_api_route("/hidden", answer, methods=["GET"], include_in_schema=False)
            else:
                leaf.mount("/attached", Starlette(routes=[Route("/private", answer)]))
            branch = APIRouter()
            branch.include_router(leaf, prefix="/nested")
            app.include_router(branch, prefix="/dynamic")
        return app

    monkeypatch.setattr(server, "create_app", create)
    if fault == "source_only":
        monkeypatch.setattr(
            acceptance, "discover_api_from_source", lambda root: (*original_source(root), *expected)
        )
    surfaces = acceptance.discover_surfaces(settings=settings)
    assert expected <= set(surfaces["api"])
    report = acceptance.classify_surfaces(surfaces, acceptance.load_matrix())
    assert expected <= set(report["unknown"])


def test_default_discovery_uses_private_closed_environment_and_cleans_up(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from tools import synthetic_live_battery as battery

    original_run = battery._run_worker_bounded
    seen = []
    poison = tmp_path / "operator"
    poison.mkdir()
    (poison / ".env.local").write_text("FRIDAY_PROFILE=poison-profile\n")
    monkeypatch.setenv("FRIDAY_HOME", str(poison))
    monkeypatch.setenv("JERICHO_DATABASE_PATH", str(poison / "private.sqlite3"))
    monkeypatch.setenv("FRIDAY_ENV_FILE", str(poison / ".env.local"))
    monkeypatch.setenv("FRIDAY_PROFILE", "poison-profile")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-provider-canary")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")

    def run(argv, **kwargs):
        environment = kwargs["env"]
        home = Path(environment["FRIDAY_HOME"])
        seen.append(home)
        assert home != poison and home.is_dir()
        assert "synthetic-provider-canary" not in json.dumps(environment)
        assert environment["FRIDAY_LLM_ENABLED"] == environment["FRIDAY_WORKERS_ENABLED"] == "0"
        assert environment["FRIDAY_ENV_FILE"] != str(poison / ".env.local")
        assert kwargs["cwd"] == home
        return original_run(argv, **kwargs)

    monkeypatch.setattr(battery, "_run_worker_bounded", run)
    surfaces = acceptance.discover_surfaces()
    assert "api:MOUNT /admin" in surfaces["api"]
    assert set(acceptance.discover_api_from_source()) <= set(surfaces["api"])
    assert len(seen) == 1 and not seen[0].exists()
    assert {path.name for path in poison.iterdir()} == {".env.local"}


@pytest.mark.parametrize("fault", ["timeout", "stdout_overflow", "identity", "network", "empty", "exception"])
def test_discovery_failure_cannot_fall_back_to_source_only_or_leak_details(
    tmp_path, monkeypatch, capsys, fault
):
    import hashlib
    import json
    from types import SimpleNamespace

    from tools import synthetic_live_battery as battery

    homes = []

    def run(_argv, **kwargs):
        from pathlib import Path

        homes.append(Path(kwargs["env"]["FRIDAY_HOME"]))
        if fault == "exception":
            raise RuntimeError("synthetic private transport detail")
        report = {
            "schema": "friday.r10-surface-discovery.v1",
            "root_sha256": hashlib.sha256(str(acceptance.ROOT).encode()).hexdigest(),
            "api": list(acceptance.discover_api_from_source()),
            "network_denied": 0,
        }
        if fault == "identity":
            report["root_sha256"] = "0" * 64
        elif fault == "network":
            report["network_denied"] = 1
        elif fault == "empty":
            report["api"] = []
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(report).encode(),
            stderr=b"synthetic private stderr",
            timed_out=fault == "timeout",
            stdout_truncated=fault == "stdout_overflow",
            stderr_truncated=False,
        )

    monkeypatch.setattr(battery, "_run_worker_bounded", run)
    path = tmp_path / "nodes.json"
    matrix = acceptance.load_matrix()
    nodes = sorted({node for case in matrix["cases"] for node in case["node_ids"]})
    path.write_text(json.dumps({"nodeids": nodes, "version": 1}, sort_keys=True, separators=(",", ":")))
    assert acceptance.main(["--audit-only", "--collection", str(path)]) == 2
    public = capsys.readouterr().out
    report = json.loads(public)
    assert report["valid"] is False and report["error"].startswith("runtime_surface_discovery_")
    assert "synthetic private" not in public
    assert homes and all(not home.exists() for home in homes)


def test_default_discovery_blocks_actual_constructor_egress(monkeypatch):
    import socket

    from tools import synthetic_live_battery as battery

    original_run = battery._run_worker_bounded
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.1)
        port = listener.getsockname()[1]

        def run(argv, **kwargs):
            command = list(argv)
            index = command.index("-c") + 1
            hook = (
                "import socket;"
                f"a.discover_api_from_runtime=lambda settings:socket.create_connection(('127.0.0.1',{port}),timeout=0.2);"
                "raise SystemExit(a._runtime_discovery_worker())"
            )
            command[index] = command[index].replace("raise SystemExit(a._runtime_discovery_worker())", hook)
            return original_run(command, **kwargs)

        monkeypatch.setattr(battery, "_run_worker_bounded", run)
        with pytest.raises(acceptance.AcceptanceError, match="runtime_surface_discovery_failed"):
            acceptance.discover_surfaces()
        # A worker returning an error is insufficient: the injected constructor
        # must not have reached even this owned model-free endpoint.
        with pytest.raises(TimeoutError):
            listener.accept()
