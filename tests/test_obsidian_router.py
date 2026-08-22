from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from friday.organs.obsidian.router import build_router

TOKEN = "A" * 43


class _Auth:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, str]] = []

    def require(self, actor: Any, capability: str) -> None:
        self.calls.append((actor, capability))


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.used_tokens: set[str] = set()

    async def _record(self, method: str, *arguments: Any) -> dict[str, Any]:
        self.calls.append((method, *arguments))
        return {"method": method, "owner": arguments[0] if arguments else ""}

    async def status(self, owner_id: str):
        return await self._record("status", owner_id)

    async def diagnostics(self, owner_id: str):
        return await self._record("diagnostics", owner_id)

    async def start(self, owner_id: str):
        return await self._record("start", owner_id)

    async def onboarding(self, owner_id: str):
        return await self._record("onboarding", owner_id)

    async def check(self, owner_id: str):
        return await self._record("check", owner_id)

    async def select_device(self, owner_id: str, candidate_id: str):
        return await self._record("select_device", owner_id, candidate_id)

    async def confirm_open(self, owner_id: str):
        return await self._record("confirm_open", owner_id)

    async def retry(self, owner_id: str):
        return await self._record("retry", owner_id)

    async def cancel(self, owner_id: str):
        return await self._record("cancel", owner_id)

    async def set_vault_alias(self, owner_id: str, alias: str):
        return await self._record("set_vault_alias", owner_id, alias)

    async def vaults(self, owner_id: str):
        return await self._record("vaults", owner_id)

    async def list_notes(self, owner_id: str):
        return await self._record("list_notes", owner_id)

    async def search_notes(self, owner_id: str, query: str, limit: int):
        return await self._record("search_notes", owner_id, query, limit)

    async def read_note(self, owner_id: str, path: str):
        return await self._record("read_note", owner_id, path)

    async def execute_operation(self, owner_id: str, body: dict[str, Any]):
        return await self._record("execute_operation", owner_id, body)

    async def get_operation(self, owner_id: str, operation_id: str):
        return await self._record("get_operation", owner_id, operation_id)

    async def resolve_public_setup(self, token: str):
        self.calls.append(("resolve_public_setup", token))
        if token in self.used_tokens:
            return None
        self.used_tokens.add(token)
        return {
            "state": "awaiting_android_device",
            "message": f"Продолжите настройку {token}",
            "server_device_id": "AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH",
            "setup_token": token,
            "user_id": "secret-owner",
        }


@pytest.fixture
def surface() -> Iterator[tuple[TestClient, FastAPI, _Runtime, _Auth, Any]]:
    app = FastAPI()
    runtime = _Runtime()
    auth = _Auth()
    actor = SimpleNamespace(own_id="person_42", user_id="shared_tenant")
    app.state.obsidian_runtime = runtime
    app.state.auth_service = auth

    @app.middleware("http")
    async def actor_middleware(request: Request, call_next):
        request.state.actor = actor
        return await call_next(request)

    app.include_router(build_router())
    with TestClient(app) as client:
        yield client, app, runtime, auth, actor


@pytest.mark.parametrize(
    ("method", "path", "runtime_method", "capability"),
    [
        ("GET", "/api/obsidian/status", "status", "obsidian.read"),
        ("GET", "/api/obsidian/diagnostics", "diagnostics", "obsidian.read"),
        ("POST", "/api/obsidian/onboarding/start", "start", "obsidian.connect"),
        ("GET", "/api/obsidian/onboarding", "onboarding", "obsidian.read"),
        ("POST", "/api/obsidian/onboarding/check", "check", "obsidian.connect"),
        ("POST", "/api/obsidian/onboarding/confirm-open", "confirm_open", "obsidian.connect"),
        ("POST", "/api/obsidian/onboarding/retry", "retry", "obsidian.connect"),
        ("POST", "/api/obsidian/onboarding/cancel", "cancel", "obsidian.connect"),
    ],
)
def test_onboarding_routes_use_the_shared_actors_own_id(
    surface, method: str, path: str, runtime_method: str, capability: str
) -> None:
    client, _app, runtime, auth, actor = surface
    response = client.request(method, path)
    assert response.status_code == 200, response.text
    assert runtime.calls[-1] == (runtime_method, "person_42")
    assert auth.calls[-1] == (actor, capability)
    assert response.json()["owner"] == "person_42"


def test_foreign_opaque_candidate_is_forwarded_only_under_the_authenticated_owner(surface) -> None:
    client, _app, runtime, auth, actor = surface
    response = client.post(
        "/api/obsidian/onboarding/select-device",
        json={"candidate_id": "obscand_from_foreign_snapshot"},
    )
    assert response.status_code == 200, response.text
    assert runtime.calls[-1] == (
        "select_device",
        "person_42",
        "obscand_from_foreign_snapshot",
    )
    assert auth.calls[-1] == (actor, "obsidian.connect")

    before = list(runtime.calls)
    rejected = client.post(
        "/api/obsidian/onboarding/select-device",
        json={"candidate_id": "obscand_other", "user_id": "foreign"},
    )
    assert rejected.status_code == 400
    assert runtime.calls == before


def test_public_setup_reads_fragment_in_external_script_and_never_echoes_the_token(surface) -> None:
    client, _app, runtime, auth, _actor = surface
    page = client.get("/obsidian/setup")
    assert page.status_code == 200
    assert TOKEN not in page.text
    assert '<script src="/obsidian/setup.js" defer></script>' in page.text
    assert "<style" not in page.text.casefold()
    assert "style=" not in page.text.casefold()
    assert "<script>" not in page.text.casefold()

    script = client.get("/obsidian/setup.js")
    assert script.status_code == 200
    assert "window.location.hash" in script.text
    assert "location.search" not in script.text
    assert "/api/public/obsidian/setup/resolve" in script.text

    auth_before = list(auth.calls)
    resolved = client.post("/api/public/obsidian/setup/resolve", json={"token": TOKEN})
    assert resolved.status_code == 200, resolved.text
    assert runtime.calls[-1] == ("resolve_public_setup", TOKEN)
    assert TOKEN not in resolved.text
    assert "token" not in resolved.json()
    assert "user_id" not in resolved.json()
    assert auth.calls == auth_before, "public token resolution must not use an actor"

    used = client.post("/api/public/obsidian/setup/resolve", json={"token": TOKEN})
    assert used.status_code == 404
    assert TOKEN not in used.text


def test_public_setup_body_and_token_are_strictly_bounded(surface) -> None:
    client, _app, runtime, _auth, _actor = surface
    before = list(runtime.calls)
    assert client.post("/api/public/obsidian/setup/resolve", json={"token": "short"}).status_code == 404
    assert (
        client.post(
            "/api/public/obsidian/setup/resolve",
            json={"token": "B" * 43, "user_id": "foreign"},
        ).status_code
        == 400
    )
    oversized = '{"token":"' + ("C" * 600) + '"}'
    assert (
        client.post(
            "/api/public/obsidian/setup/resolve",
            content=oversized,
            headers={"Content-Type": "application/json"},
        ).status_code
        == 413
    )
    assert runtime.calls == before


def test_public_open_launcher_uses_only_a_fragment_and_accepts_a_safe_exact_note_path(surface) -> None:
    client, _app, _runtime, auth, _actor = surface
    auth_before = list(auth.calls)
    page = client.get("/obsidian/open")
    script = client.get("/obsidian/open.js")

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert '<script src="/obsidian/open.js" defer></script>' in page.text
    assert script.status_code == 200
    assert "window.location.hash" in script.text
    assert "location.search" not in script.text
    assert "window.location.assign" in script.text
    assert "obsidian://open?" in script.text
    assert 'const fileParts = file.split("/")' in script.text
    assert 'file.startsWith("/")' in script.text
    assert 'part === ".."' in script.text
    assert 'lowerFile.endsWith(".md")' in script.text
    assert 'lowerFile.endsWith(".base")' in script.text
    assert 'file !== "Friday Connection Test.md"' not in script.text
    assert "link.textContent" in script.text
    assert auth.calls == auth_before


def test_vault_alias_is_owner_scoped_and_has_an_exact_body(surface) -> None:
    client, _app, runtime, auth, actor = surface
    response = client.post("/api/obsidian/onboarding/vault-alias", json={"alias": "Личный vault"})
    assert response.status_code == 200, response.text
    assert runtime.calls[-1] == ("set_vault_alias", "person_42", "Личный vault")
    assert auth.calls[-1] == (actor, "obsidian.connect")

    before = list(runtime.calls)
    assert (
        client.post(
            "/api/obsidian/onboarding/vault-alias",
            json={"alias": "Friday", "owner_id": "foreign"},
        ).status_code
        == 400
    )
    assert runtime.calls == before


def test_vaults_use_the_actor_owner_and_explicit_owner_inputs_are_rejected(surface) -> None:
    client, _app, runtime, auth, actor = surface
    response = client.get("/api/obsidian/vaults")
    assert response.status_code == 200, response.text
    assert runtime.calls[-1] == ("vaults", "person_42")
    assert auth.calls[-1] == (actor, "obsidian.read")

    before = list(runtime.calls)
    assert client.get("/api/obsidian/status?user_id=foreign").status_code == 400
    assert client.get("/api/obsidian/vaults?owner_id=foreign").status_code == 400
    assert client.post("/api/obsidian/onboarding/start", json={"user_id": "foreign"}).status_code == 400
    assert runtime.calls == before


def test_note_reads_and_search_are_owner_scoped_and_bounded(surface) -> None:
    client, _app, runtime, auth, actor = surface
    assert client.get("/api/obsidian/notes").status_code == 200
    assert runtime.calls[-1] == ("list_notes", "person_42")

    searched = client.get("/api/obsidian/notes/search", params={"q": "архитектура", "limit": 7})
    assert searched.status_code == 200, searched.text
    assert runtime.calls[-1] == ("search_notes", "person_42", "архитектура", 7)

    read = client.get("/api/obsidian/notes/read", params={"path": "Projects/Friday.md"})
    assert read.status_code == 200, read.text
    assert runtime.calls[-1] == ("read_note", "person_42", "Projects/Friday.md")
    assert auth.calls[-1] == (actor, "obsidian.read")

    before = list(runtime.calls)
    assert client.get("/api/obsidian/notes/search?q=x&limit=101").status_code == 400
    assert client.get("/api/obsidian/notes/read?path=x&owner_id=foreign").status_code == 400
    assert runtime.calls == before


def test_operation_routes_never_accept_an_explicit_owner(surface) -> None:
    client, _app, runtime, auth, actor = surface
    body = {
        "method": "append",
        "operation_id": "obsop_1",
        "path": "Inbox.md",
        "text": "result",
    }
    written = client.post("/api/obsidian/operations", json=body)
    assert written.status_code == 200, written.text
    assert runtime.calls[-1] == ("execute_operation", "person_42", body)
    assert auth.calls[-1] == (actor, "obsidian.write")

    fetched = client.get("/api/obsidian/operations/obsop_1")
    assert fetched.status_code == 200, fetched.text
    assert runtime.calls[-1] == ("get_operation", "person_42", "obsop_1")
    assert auth.calls[-1] == (actor, "obsidian.read")

    before = list(runtime.calls)
    assert client.post("/api/obsidian/operations", json={**body, "owner_id": "foreign"}).status_code == 400
    assert client.get("/api/obsidian/operations/obsop_1?user_id=foreign").status_code == 400
    assert runtime.calls == before
