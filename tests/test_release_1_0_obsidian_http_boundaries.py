from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from friday.organs.obsidian.router import build_router
from friday.server import create_app

_OPERATION_BODY_BYTES = 256 * 1024

_ALL_OBSIDIAN_ROUTES = frozenset(
    {
        ("GET", "/api/obsidian/status"),
        ("GET", "/api/obsidian/diagnostics"),
        ("POST", "/api/obsidian/onboarding/start"),
        ("GET", "/api/obsidian/onboarding"),
        ("POST", "/api/obsidian/onboarding/check"),
        ("POST", "/api/obsidian/onboarding/select-device"),
        ("POST", "/api/obsidian/onboarding/confirm-open"),
        ("POST", "/api/obsidian/onboarding/retry"),
        ("POST", "/api/obsidian/onboarding/cancel"),
        ("POST", "/api/obsidian/onboarding/vault-alias"),
        ("GET", "/api/obsidian/vaults"),
        ("GET", "/api/obsidian/notes"),
        ("GET", "/api/obsidian/notes/search"),
        ("GET", "/api/obsidian/notes/read"),
        ("POST", "/api/obsidian/operations"),
        ("GET", "/api/obsidian/operations/{operation_id}"),
        ("GET", "/obsidian/setup"),
        ("GET", "/obsidian/setup.js"),
        ("GET", "/obsidian/open"),
        ("GET", "/obsidian/open.js"),
        ("POST", "/api/public/obsidian/setup/resolve"),
    }
)

_OWNER_GET_CASES = (
    ("status", "/api/obsidian/status", ()),
    ("diagnostics", "/api/obsidian/diagnostics", ()),
    ("onboarding", "/api/obsidian/onboarding", ()),
    ("vaults", "/api/obsidian/vaults", ()),
    ("notes", "/api/obsidian/notes", ()),
    ("search", "/api/obsidian/notes/search", (("q", "bounded"),)),
    ("read", "/api/obsidian/notes/read", (("path", "Inbox.md"),)),
    ("operation", "/api/obsidian/operations/obsop_1", ()),
)

_OWNER_HTTP_CASES = (
    ("status", "GET", "/api/obsidian/status", {}),
    ("diagnostics", "GET", "/api/obsidian/diagnostics", {}),
    ("start", "POST", "/api/obsidian/onboarding/start", {}),
    ("onboarding", "GET", "/api/obsidian/onboarding", {}),
    ("check", "POST", "/api/obsidian/onboarding/check", {}),
    (
        "select-device",
        "POST",
        "/api/obsidian/onboarding/select-device",
        {"json": {"candidate_id": "candidate_1"}},
    ),
    ("confirm-open", "POST", "/api/obsidian/onboarding/confirm-open", {}),
    ("retry", "POST", "/api/obsidian/onboarding/retry", {}),
    ("cancel", "POST", "/api/obsidian/onboarding/cancel", {}),
    (
        "vault-alias",
        "POST",
        "/api/obsidian/onboarding/vault-alias",
        {"json": {"alias": "Friday"}},
    ),
    ("vaults", "GET", "/api/obsidian/vaults", {}),
    ("notes", "GET", "/api/obsidian/notes", {}),
    (
        "search",
        "GET",
        "/api/obsidian/notes/search",
        {"params": {"q": "bounded", "limit": "20"}},
    ),
    (
        "read",
        "GET",
        "/api/obsidian/notes/read",
        {"params": {"path": "Inbox.md"}},
    ),
    (
        "execute-operation",
        "POST",
        "/api/obsidian/operations",
        {"json": {"method": "append", "path": "Inbox.md", "text": "x"}},
    ),
    ("get-operation", "GET", "/api/obsidian/operations/obsop_1", {}),
)


class _Auth:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, str]] = []

    def require(self, actor: Any, capability: str) -> None:
        self.calls.append((actor, capability))


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def _record(self, method: str, *arguments: Any) -> dict[str, Any]:
        self.calls.append((method, *arguments))
        return {"method": method, "owner": arguments[0] if arguments else ""}

    async def status(self, owner_id: str):
        return await self._record("status", owner_id)

    async def diagnostics(self, owner_id: str):
        return await self._record("diagnostics", owner_id)

    async def onboarding(self, owner_id: str):
        return await self._record("onboarding", owner_id)

    async def vaults(self, owner_id: str):
        return await self._record("vaults", owner_id)

    async def list_notes(self, owner_id: str):
        return await self._record("list_notes", owner_id)

    async def search_notes(self, owner_id: str, query: str, limit: int):
        return await self._record("search_notes", owner_id, query, limit)

    async def read_note(self, owner_id: str, path: str):
        return await self._record("read_note", owner_id, path)

    async def select_device(self, owner_id: str, candidate_id: str):
        self.calls.append(("select_device", owner_id, candidate_id))
        if candidate_id == "missing_candidate":
            raise ValueError("untrusted runtime message")
        return {"method": "select_device", "owner": owner_id}

    async def execute_operation(self, owner_id: str, body: dict[str, Any]):
        return await self._record("execute_operation", owner_id, body)

    async def get_operation(self, owner_id: str, operation_id: str):
        return await self._record("get_operation", owner_id, operation_id)


@pytest.fixture
def surface() -> Iterator[tuple[TestClient, _Runtime, _Auth, Any]]:
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
        yield client, runtime, auth, actor


@pytest.mark.parametrize(
    "body",
    [
        {"candidate_id": ""},
        {"candidate_id": "contains/slash"},
        {"candidate_id": "contains space"},
        {"candidate_id": "x" * 97},
        {"candidate_id": 42},
    ],
    ids=("empty", "slash", "space", "length-97", "non-string"),
)
def test_malformed_candidate_id_is_refused_before_authority_or_runtime(surface, body: dict[str, Any]) -> None:
    client, runtime, auth, _actor = surface

    response = client.post("/api/obsidian/onboarding/select-device", json=body)

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid candidate_id"}
    assert runtime.calls == []
    assert auth.calls == []


def test_unknown_well_formed_candidate_has_a_stable_404_after_owner_binding(surface) -> None:
    client, runtime, auth, actor = surface

    response = client.post(
        "/api/obsidian/onboarding/select-device",
        json={"candidate_id": "missing_candidate"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Obsidian device candidate not found"}
    assert runtime.calls == [("select_device", "person_42", "missing_candidate")]
    assert auth.calls == [(actor, "obsidian.connect")]
    assert "untrusted runtime message" not in response.text


def test_candidate_id_exact_regex_upper_bound_is_forwarded_under_the_owner(surface) -> None:
    client, runtime, auth, actor = surface
    candidate_id = ("A._-" * 24)[:96]

    response = client.post(
        "/api/obsidian/onboarding/select-device",
        json={"candidate_id": candidate_id},
    )

    assert response.status_code == 200
    assert runtime.calls == [("select_device", "person_42", candidate_id)]
    assert auth.calls == [(actor, "obsidian.connect")]


@pytest.mark.parametrize(
    ("path", "detail"),
    [
        (
            "/api/obsidian/notes?unexpected=1",
            "Note list accepts no query fields",
        ),
        (
            "/api/obsidian/notes/search",
            "q is required exactly once",
        ),
        (
            "/api/obsidian/notes/search?q=one&q=two",
            "q is required exactly once",
        ),
        (
            "/api/obsidian/notes/search?q=",
            "q must be non-empty and bounded",
        ),
        (
            "/api/obsidian/notes/search?q=" + ("q" * 1001),
            "q must be non-empty and bounded",
        ),
        (
            "/api/obsidian/notes/search?q=x&unexpected=1",
            "Unsupported note search field",
        ),
        (
            "/api/obsidian/notes/search?q=x&limit=not-an-int",
            "limit must be an integer",
        ),
        (
            "/api/obsidian/notes/search?q=x&limit=0",
            "limit must be between 1 and 100",
        ),
        (
            "/api/obsidian/notes/search?q=x&limit=101",
            "limit must be between 1 and 100",
        ),
        (
            "/api/obsidian/notes/search?q=x&limit=1&limit=2",
            "limit must be between 1 and 100",
        ),
        (
            "/api/obsidian/notes/read",
            "path is required exactly once",
        ),
        (
            "/api/obsidian/notes/read?path=one&path=two",
            "path is required exactly once",
        ),
        (
            "/api/obsidian/notes/read?path=x&unexpected=1",
            "path is required exactly once",
        ),
        (
            "/api/obsidian/notes/read?path=",
            "path must be non-empty and bounded",
        ),
        (
            "/api/obsidian/notes/read?path=" + ("p" * 2049),
            "path must be non-empty and bounded",
        ),
    ],
    ids=(
        "list-field",
        "search-missing-q",
        "search-duplicate-q",
        "search-empty-q",
        "search-q-length-1001",
        "search-extra-field",
        "search-limit-type",
        "search-limit-zero",
        "search-limit-101",
        "search-duplicate-limit",
        "read-missing-path",
        "read-duplicate-path",
        "read-extra-field",
        "read-empty-path",
        "read-path-length-2049",
    ),
)
def test_note_query_grammar_refuses_every_outside_value_before_owner_binding(
    surface, path: str, detail: str
) -> None:
    client, runtime, auth, _actor = surface

    response = client.get(path)

    assert response.status_code == 400
    assert response.json() == {"detail": detail}
    assert runtime.calls == []
    assert auth.calls == []


def test_note_query_exact_upper_and_lower_bounds_are_forwarded_under_the_owner(
    surface,
) -> None:
    client, runtime, auth, actor = surface

    lower = client.get(
        "/api/obsidian/notes/search",
        params={"q": "q", "limit": "1"},
    )
    upper = client.get(
        "/api/obsidian/notes/search",
        params={"q": "q" * 1000, "limit": "100"},
    )
    read = client.get(
        "/api/obsidian/notes/read",
        params={"path": "p" * 2048},
    )

    assert lower.status_code == upper.status_code == read.status_code == 200
    assert runtime.calls == [
        ("search_notes", "person_42", "q", 1),
        ("search_notes", "person_42", "q" * 1000, 100),
        ("read_note", "person_42", "p" * 2048),
    ]
    assert auth.calls == [
        (actor, "obsidian.read"),
        (actor, "obsidian.read"),
        (actor, "obsidian.read"),
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/api/obsidian/onboarding/start",
        "/api/obsidian/onboarding/check",
        "/api/obsidian/onboarding/confirm-open",
        "/api/obsidian/onboarding/retry",
        "/api/obsidian/onboarding/cancel",
    ],
    ids=("start", "check", "confirm-open", "retry", "cancel"),
)
def test_every_empty_body_connect_post_refuses_nonempty_json_before_authority(surface, path: str) -> None:
    client, runtime, auth, _actor = surface

    response = client.post(path, json={"unexpected": True})

    assert response.status_code == 400
    assert response.json() == {"detail": "This action accepts no fields"}
    assert runtime.calls == []
    assert auth.calls == []


@pytest.mark.parametrize("owner_key", ["user_id", "owner_id"], ids=("user", "owner"))
@pytest.mark.parametrize(
    ("_case", "path", "base_params"),
    _OWNER_GET_CASES,
    ids=[case[0] for case in _OWNER_GET_CASES],
)
def test_every_owner_get_refuses_each_explicit_owner_key_before_authority(
    surface,
    _case: str,
    path: str,
    base_params: tuple[tuple[str, str], ...],
    owner_key: str,
) -> None:
    client, runtime, auth, _actor = surface

    response = client.get(path, params=(*base_params, (owner_key, "foreign")))

    assert response.status_code == 400
    assert response.json() == {"detail": "Explicit Obsidian owner is not accepted"}
    assert runtime.calls == []
    assert auth.calls == []


@pytest.mark.parametrize(
    ("path", "status", "detail"),
    [
        ("/api/obsidian/operations/", 405, "Method Not Allowed"),
        (
            "/api/obsidian/operations/" + ("o" * 201),
            400,
            "Invalid operation_id",
        ),
        ("/api/obsidian/operations/%00", 400, "Invalid operation_id"),
    ],
    ids=("empty", "length-201", "nul"),
)
def test_operation_id_outside_bounds_is_refused_without_runtime(
    surface, path: str, status: int, detail: str
) -> None:
    client, runtime, auth, _actor = surface

    response = client.get(path)

    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert runtime.calls == []
    assert auth.calls == []


def test_operation_id_exact_upper_bound_is_forwarded_under_the_owner(surface) -> None:
    client, runtime, auth, actor = surface
    operation_id = "o" * 200

    response = client.get(f"/api/obsidian/operations/{operation_id}")

    assert response.status_code == 200
    assert runtime.calls == [("get_operation", "person_42", operation_id)]
    assert auth.calls == [(actor, "obsidian.read")]


def test_operation_body_exact_byte_limit_passes_and_next_byte_is_refused(surface) -> None:
    client, runtime, auth, actor = surface
    prefix = b'{"blob":"'
    suffix = b'"}'
    exact = prefix + (b"x" * (_OPERATION_BODY_BYTES - len(prefix) - len(suffix))) + suffix
    headers = {"Content-Type": "application/json"}

    accepted = client.post("/api/obsidian/operations", content=exact, headers=headers)

    assert len(exact) == _OPERATION_BODY_BYTES
    assert accepted.status_code == 200, accepted.text
    assert runtime.calls[0][0:2] == ("execute_operation", "person_42")
    assert runtime.calls[0][2]["blob"] == "x" * (_OPERATION_BODY_BYTES - len(prefix) - len(suffix))
    assert auth.calls == [(actor, "obsidian.write")]

    refused = client.post(
        "/api/obsidian/operations",
        content=exact + b" ",
        headers=headers,
    )

    assert refused.status_code == 413
    assert refused.json() == {"detail": "Request body is too large"}
    assert len(runtime.calls) == 1
    assert auth.calls == [(actor, "obsidian.write")]


def _enabled(settings):
    return replace(
        settings,
        obsidian_enabled=True,
        obsidian_root=settings.data_dir / "obsidian-boundaries",
        obsidian_syncthing_binary="/bin/dash",
        obsidian_public_base_url="https://friday.example",
    )


def _obsidian_route_identities(app) -> set[tuple[str, str]]:
    identities: set[tuple[str, str]] = set()

    def walk(routes, prefix: str = "") -> None:
        for route in routes:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                context = getattr(route, "include_context", None)
                walk(nested.routes, prefix + getattr(context, "prefix", ""))
                continue
            path = getattr(route, "path", None)
            if path is None:
                continue
            full_path = prefix + str(path)
            if not (
                full_path.startswith("/api/obsidian/")
                or full_path.startswith("/api/public/obsidian/")
                or full_path.startswith("/obsidian/")
            ):
                continue
            for method in set(getattr(route, "methods", ())) & {"GET", "POST"}:
                identities.add((method, full_path))

    walk(app.routes)
    return identities


def test_server_has_exactly_all_21_obsidian_routes_enabled_and_none_disabled(
    settings,
) -> None:
    assert _obsidian_route_identities(create_app(settings)) == set()
    assert _obsidian_route_identities(create_app(_enabled(settings))) == set(_ALL_OBSIDIAN_ROUTES)


@pytest.mark.parametrize(
    ("_case", "method", "path", "request_kwargs"),
    _OWNER_HTTP_CASES,
    ids=[case[0] for case in _OWNER_HTTP_CASES],
)
def test_every_owner_http_route_refuses_an_unauthenticated_request(
    settings,
    _case: str,
    method: str,
    path: str,
    request_kwargs: dict[str, Any],
) -> None:
    with TestClient(create_app(_enabled(settings))) as client:
        response = client.request(method, path, **request_kwargs)

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing authentication"}
