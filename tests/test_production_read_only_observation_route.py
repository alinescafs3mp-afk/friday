"""Hidden owner-only transport for the production read-only observation."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

_PATH = "/api/admin/production-read-only-observation"
_CHALLENGE_HEADER = "X-Friday-Production-Observation-Challenge-SHA256"
_CHALLENGE = "a" * 64


def _owner_headers(settings, *challenge_values: str) -> list[tuple[str, str]]:
    return [
        ("Authorization", f"Bearer {settings.api_token}"),
        *((_CHALLENGE_HEADER, value) for value in challenge_values),
    ]


def test_real_lifespan_collector_uses_the_existing_storage_connection(settings) -> None:
    from friday.permissions import LEGACY_OWNER_USER_ID
    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app, client=("127.0.0.1", 9000)) as client:
        owner_before = app.state.storage.get_user(LEGACY_OWNER_USER_ID)
        response = client.get(_PATH, headers=_owner_headers(settings, _CHALLENGE))
        owner_after = app.state.storage.get_user(LEGACY_OWNER_USER_ID)

    assert response.status_code == 200
    assert owner_before is not None
    assert owner_after == owner_before
    assert response.headers["content-type"] == "application/json"
    payload = response.json()
    assert payload["schema"] == "friday.production-read-only-observation.v1"
    assert payload["challenge_sha256"] == _CHALLENGE
    assert payload["backend_lease_owned"] is True
    assert payload["database"]["schema_version"] == 50
    assert payload["database"]["integrity"] == "ok"
    assert payload["database"]["foreign_key_violations"] == 0
    assert payload["hard_contradictions"] == 0
    assert response.content == json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def test_route_rejects_implicit_loopback_and_noncanonical_bearers_before_collection(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.admin_api import _overview
    from friday.server import create_app

    calls = 0

    def collect(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("noncanonical authentication reached the collector")

    monkeypatch.setattr(_overview, "collect_production_read_only_observation", collect)
    app = create_app(settings)
    with TestClient(app, client=("127.0.0.1", 9000)) as client:
        implicit = client.get(_PATH, headers={_CHALLENGE_HEADER: _CHALLENGE})
        foreign = client.get(
            _PATH,
            headers={
                "Authorization": "Bearer scoped-owner-token-placeholder",
                _CHALLENGE_HEADER: _CHALLENGE,
            },
        )
        duplicate = client.get(
            _PATH,
            headers=[
                ("Authorization", f"Bearer {settings.api_token}"),
                ("Authorization", f"Bearer {settings.api_token}"),
                (_CHALLENGE_HEADER, _CHALLENGE),
            ],
        )

    assert implicit.status_code == foreign.status_code == duplicate.status_code == 401
    assert calls == 0


def test_owner_loopback_receives_exact_canonical_bytes_and_content_type(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.admin_api import _overview
    from friday.server import create_app

    canonical = (
        b'{"challenge_sha256":"'
        + _CHALLENGE.encode("ascii")
        + b'","schema":"friday.production-read-only-observation.v1"}'
    )
    calls: list[tuple[object, object, str]] = []

    class Observation:
        def canonical_bytes(self) -> bytes:
            return canonical

    def collect(actual_settings, storage, *, challenge_sha256: str) -> Observation:
        calls.append((actual_settings, storage, challenge_sha256))
        return Observation()

    monkeypatch.setattr(_overview, "collect_production_read_only_observation", collect)
    app = create_app(settings)
    with TestClient(app, client=("127.0.0.1", 9000)) as client:
        response = client.get(_PATH, headers=_owner_headers(settings, _CHALLENGE))
        observed_storage = app.state.storage

    assert response.status_code == 200
    assert response.content == canonical
    assert response.headers["content-type"] == "application/json"
    assert response.json() == json.loads(canonical)
    assert calls == [(settings, observed_storage, _CHALLENGE)]


def test_delegate_and_non_numeric_or_remote_peers_are_denied_before_collection(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.admin_api import _overview
    from friday.permissions import ActorContext
    from friday.server import create_app

    calls = 0

    def collect(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("unauthorized request reached the collector")

    monkeypatch.setattr(_overview, "collect_production_read_only_observation", collect)

    class AllowingAuth:
        def require(self, *_args, **_kwargs) -> None:
            pass

    unauthorized_actors = (
        ActorContext(
            "tenant",
            "owner",
            "api",
            shared_tenant=True,
            person_id="delegate",
        ),
        ActorContext("owner", "owner", "loopback"),
        ActorContext("owner", "owner", "api-token", identity_id="scoped-owner-token"),
        ActorContext("owner", "owner", "api-token", identity_id="owner-token-alias"),
        ActorContext("foreign-owner", "owner", "api-token", identity_id="owner-token"),
    )
    for actor in unauthorized_actors:
        unauthorized = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(auth_service=AllowingAuth())),
            state=SimpleNamespace(actor=actor),
            client=SimpleNamespace(host="127.0.0.1"),
            headers=Headers({_CHALLENGE_HEADER: _CHALLENGE}),
        )
        with pytest.raises(HTTPException) as failure:
            _overview._production_read_only_observation_sync(unauthorized)  # noqa: SLF001
        assert failure.value.status_code == 403
        assert failure.value.detail == "Production-наблюдение доступно только владельцу"

    for peer in ("203.0.113.9", "localhost"):
        app = create_app(settings)
        with TestClient(app, client=(peer, 9000)) as client:
            response = client.get(_PATH, headers=_owner_headers(settings, _CHALLENGE))
        assert response.status_code == 403
        assert response.json() == {"detail": "Production-наблюдение доступно только локально на сервере"}
    assert calls == 0


def test_existing_document_contour_route_keeps_legacy_owner_alias_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.admin_api import _overview
    from friday.permissions import ActorContext

    expected = {"schema": "legacy-document-contour-regression"}
    calls: list[tuple[object, object]] = []

    class AllowingAuth:
        def require(self, *_args, **_kwargs) -> None:
            pass

    settings = object()
    storage = object()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_service=AllowingAuth(),
                settings=settings,
                storage=storage,
            )
        ),
        state=SimpleNamespace(actor=ActorContext("owner", "owner", "loopback")),
        client=SimpleNamespace(host="127.0.0.1"),
    )

    def collect(actual_settings, actual_storage):
        calls.append((actual_settings, actual_storage))
        return expected

    monkeypatch.setattr(_overview, "collect_document_contour_observer_snapshot", collect)

    assert _overview._document_contour_observer_snapshot_sync(request) is expected  # noqa: SLF001
    assert calls == [(settings, storage)]


@pytest.mark.parametrize(
    "challenge_values",
    (
        pytest.param((), id="missing"),
        pytest.param(("not-a-digest",), id="malformed"),
        pytest.param(("A" * 64,), id="uppercase"),
        pytest.param(("0" * 64,), id="zero-placeholder"),
        pytest.param((_CHALLENGE, "b" * 64), id="duplicate"),
    ),
)
def test_challenge_header_is_exactly_one_lowercase_digest_and_is_never_echoed(
    settings,
    monkeypatch: pytest.MonkeyPatch,
    challenge_values: tuple[str, ...],
) -> None:
    from friday.admin_api import _overview
    from friday.server import create_app

    calls = 0

    def collect(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("invalid challenge reached the collector")

    monkeypatch.setattr(_overview, "collect_production_read_only_observation", collect)
    app = create_app(settings)
    with TestClient(app, client=("127.0.0.1", 9000)) as client:
        response = client.get(_PATH, headers=_owner_headers(settings, *challenge_values))

    assert response.status_code == 400
    assert response.json() == {"detail": "Некорректный challenge production-наблюдения"}
    assert calls == 0
    for value in challenge_values:
        assert value not in response.text


def test_collector_uncertainty_is_one_generic_503_without_private_detail(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.admin_api import _overview
    from friday.server import create_app

    private_detail = f"PRIVATE-COLLECTOR-FAILURE-{_CHALLENGE}"

    def collect(*_args, **_kwargs):
        raise RuntimeError(private_detail)

    monkeypatch.setattr(_overview, "collect_production_read_only_observation", collect)
    app = create_app(settings)
    with TestClient(app, client=("127.0.0.1", 9000)) as client:
        response = client.get(_PATH, headers=_owner_headers(settings, _CHALLENGE))

    assert response.status_code == 503
    assert response.json() == {"detail": "Production-наблюдение недоступно"}
    assert private_detail not in response.text
    assert _CHALLENGE not in response.text


def test_route_and_challenge_carrier_are_absent_from_openapi(settings) -> None:
    from friday.server import create_app

    schema = create_app(settings).openapi()
    encoded = json.dumps(schema, ensure_ascii=True, sort_keys=True).casefold()

    assert _PATH not in schema["paths"]
    assert _PATH not in encoded
    assert _CHALLENGE_HEADER.casefold() not in encoded
    assert _CHALLENGE not in encoded
