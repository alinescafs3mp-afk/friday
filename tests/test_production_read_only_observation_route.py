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
    import hashlib
    import os
    import sqlite3
    from contextlib import closing
    from pathlib import Path

    from friday.permissions import LEGACY_OWNER_USER_ID
    from friday.server import create_app
    from friday.storage.models import AuditEntry, Mission, MissionStatus, MissionTask, TaskKind, new_id

    def snapshot():
        # Fresh SQL reads, independent of the collector's aggregate projection.
        with closing(sqlite3.connect(settings.database_path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            return {
                table: [dict(row) for row in conn.execute(f'SELECT rowid, * FROM "{table}" ORDER BY rowid')]
                for table in (
                    "missions",
                    "mission_tasks",
                    "outbound_notifications",
                    "runtime_kv",
                    "schema_meta",
                    "users",
                    "audit_log",
                )
            }

    def process_epoch():
        # Observe Linux identity directly; never ask the production digest helper.
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        process_id = os.getpid()
        raw_stat = Path(f"/proc/{process_id}/stat").read_text(encoding="ascii")
        suffix = raw_stat[raw_stat.rindex(")") + 2 :].split()
        assert suffix[19].isdigit()
        assert Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip() == boot
        return hashlib.sha256(
            b"friday.primary-process-epoch.v2\0"
            + boot.encode("ascii")
            + b"\0"
            + str(process_id).encode("ascii")
            + b"\0"
            + suffix[19].encode("ascii")
        ).hexdigest()

    app = create_app(settings)
    with TestClient(app, client=("127.0.0.1", 9000)) as client:
        storage = app.state.storage
        tenant = "observation-other-tenant"
        storage.ensure_user(tenant)
        mission = Mission(
            id=new_id("mis"),
            user_id=tenant,
            goal="PRIVATE-OBS-MISSION-081",
            status=MissionStatus.READY,
            created_by=tenant,
        )
        storage.create_mission(mission)
        task = MissionTask(
            id=new_id("mst"),
            mission_id=mission.id,
            user_id=tenant,
            seq=1,
            kind=TaskKind.GATHER,
            instruction="PRIVATE-OBS-TASK-081",
        )
        assert (
            storage.set_mission_plan(
                mission.id,
                tenant,
                [task],
                plan_summary="PRIVATE-OBS-PLAN-081",
                status=MissionStatus.READY,
            )
            is not None
        )
        assert storage.enqueue_notification(
            tenant,
            "5001",
            "PRIVATE-OBS-REMINDER-081",
            kind="reminder",
            dedup_key="reminder:observation:pending",
        )
        assert storage.silence_reminder(tenant, "reminder:observation:dismissed", chat_id="5001")
        assert storage.enqueue_notification(
            tenant,
            "5001",
            "PRIVATE-OBS-GENERAL-081",
            kind="general",
            dedup_key="observation:general",
        )
        health = {
            "workers:health:mission_runner": {"status": "ok", "error": "PRIVATE-OBS-WORKER-081"},
            "workers:health:reminders_scan": {"status": "running", "detail": "PRIVATE-OBS-DETAIL-081"},
            "workers:health:scheduled_backup": {"status": "error", "error": "PRIVATE-OBS-UNRELATED-081"},
        }
        for key, value in health.items():
            storage.kv_set(key, json.dumps(value, sort_keys=True))
        storage.log_audit(
            AuditEntry(
                id=new_id("aud"),
                user_id=tenant,
                action="mission.created",
                target_type="mission",
                target_id=mission.id,
                after_json={"status": "ready"},
            )
        )
        before = snapshot()
        assert [
            (row["id"], row["user_id"], row["status"], row["task_count"]) for row in before["missions"]
        ] == [(mission.id, tenant, "ready", 1)]
        assert [
            (row["id"], row["mission_id"], row["user_id"], row["status"]) for row in before["mission_tasks"]
        ] == [(task.id, mission.id, tenant, "pending")]
        assert sorted(
            (row["kind"], row["status"], row["user_id"]) for row in before["outbound_notifications"]
        ) == [
            ("general", "pending", tenant),
            ("reminder", "dismissed", tenant),
            ("reminder", "pending", tenant),
        ]
        assert {
            row["key"]: json.loads(row["value"])
            for row in before["runtime_kv"]
            if row["key"].startswith("workers:health:")
        } == health
        assert before["audit_log"]
        # This unregistered fixture action is deliberately sanitized by log_audit.
        assert [(row["user_id"], row["action"], row["target_id"]) for row in before["audit_log"]] == [
            (tenant, "audit.unknown", mission.id)
        ]
        owner_before = storage.get_user(LEGACY_OWNER_USER_ID)
        epoch = process_epoch()
        expected = {
            "schema": "friday.production-read-only-observation.v1",
            "challenge_sha256": _CHALLENGE,
            "backend_process_epoch_sha256": epoch,
            "backend_lease_owned": True,
            "database": {
                "schema_version": 50,
                "schema_attestation_sha256": "726ded0b802ee1c6bf82663fd0918efb7f3d509f382c0d2aaa3540d4a1790561",
                "integrity": "ok",
                "foreign_key_violations": 0,
            },
            "scheduled_work": {
                "missions": {
                    "proposed": 0,
                    "ready": 1,
                    "running": 0,
                    "paused": 0,
                    "blocked": 0,
                    "completed": 0,
                    "failed": 0,
                    "cancelled": 0,
                },
                "mission_tasks": {
                    "pending": 1,
                    "running": 0,
                    "done": 0,
                    "failed": 0,
                    "skipped": 0,
                    "uncertain": 0,
                    "compensated": 0,
                },
                "reminders": {"pending": 1, "uncertain": 0, "sent": 0, "failed": 0, "dismissed": 1},
                "workers": {
                    "present": 2,
                    "missing": 0,
                    "health_states": {
                        "scheduled": 0,
                        "running": 1,
                        "ok": 1,
                        "error": 0,
                        "timeout": 0,
                        "skipped": 0,
                        "unknown": 0,
                    },
                },
            },
            "hard_contradictions": 0,
        }
        response = client.get(_PATH, headers=_owner_headers(settings, _CHALLENGE))
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.json() == expected
        # Comparing expected bytes also rejects Python's True == 1 and float == int.
        assert response.content == json.dumps(
            expected,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        assert snapshot() == before
        assert process_epoch() == epoch
        for forbidden in (
            "PRIVATE-OBS-",
            tenant,
            mission.id,
            task.id,
            "mission_runner",
            "reminders_scan",
            "scheduled_backup",
            str(settings.database_path),
            str(settings.state_dir),
        ):
            assert forbidden.encode() not in response.content

        refused = client.get(_PATH, headers=_owner_headers(settings))
        assert refused.status_code == 400
        assert refused.json() == {"detail": "Некорректный challenge production-наблюдения"}
        assert snapshot() == before
        owner_after = storage.get_user(LEGACY_OWNER_USER_ID)

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
