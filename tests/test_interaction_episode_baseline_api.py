"""Admin baseline reports expose aggregate episode shape and no retained trace data."""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from friday.interaction_control_plane import FailureStage
from friday.interaction_control_plane.failure_store import (
    INTERACTION_FAILURE_REPORT_LIMIT,
    FailureEntrypoint,
    FailureRoute,
    FailureTraceScope,
    record_precommit_failure,
)

_PATH = "/api/admin/eval/interaction-episode-baseline"


def _seed_private_failure(storage, user_id: str) -> tuple[str, dict[str, str]]:
    storage.ensure_user(user_id, source="test")
    conversation = storage.create_conversation(user_id, "PRIVATE BASELINE TITLE 4821")
    scope = FailureTraceScope(
        user_id=user_id,
        conversation_id=conversation["id"],
        entrypoint=FailureEntrypoint.API_CHAT,
        route=FailureRoute.ARCHIVE_READ,
        stage=FailureStage.CAPABILITY,
        turn_identifier="PRIVATE TURN IDENTIFIER 4821",
    )
    assert record_precommit_failure(storage, scope, RuntimeError("PRIVATE FAILURE BODY 4821"))
    row = storage.execute(
        """SELECT turn_digest,conversation_digest,trace_json
             FROM interaction_failure_traces WHERE user_id=?""",
        (user_id,),
    ).fetchone()
    assert row is not None
    return str(conversation["id"]), dict(row)


def _issue_user_token(storage, user_id: str, secret: str) -> dict[str, str]:
    storage.ensure_user(user_id, source="test", preset_key="user")
    storage.update_user(user_id, preset_key="user")
    storage.create_api_token(
        user_id,
        hashlib.sha256(secret.encode("utf-8")).hexdigest(),
        label="baseline-test",
        created_by="test",
    )
    return {"Authorization": f"Bearer {secret}"}


def test_episode_baseline_endpoint_is_bounded_body_free_and_cross_tenant_audited(settings) -> None:
    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        target = "local:baseline-target"
        conversation_id, retained = _seed_private_failure(app.state.storage, target)
        response = client.get(
            _PATH,
            params={
                "user_id": target,
                "since": "2020-01-01T00:00:00+00:00",
                "limit": 7,
            },
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"user_id", "since", "limit", "report"}
        assert payload["user_id"] == target
        assert payload["since"] == "2020-01-01T00:00:00+00:00"
        assert payload["limit"] == 7
        report = payload["report"]
        assert report["schema"] == "friday.interaction-episode-baseline.v1"
        assert report["observed_turns"] == 1
        assert report["precommit_failures"] == 1
        assert report["failure_reasons"] == {"internal_error": 1}

        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        for private_value in (
            "PRIVATE BASELINE TITLE 4821",
            "PRIVATE TURN IDENTIFIER 4821",
            "PRIVATE FAILURE BODY 4821",
            conversation_id,
            retained["turn_digest"],
            retained["conversation_digest"],
            retained["trace_json"],
        ):
            assert private_value not in serialized
        assert not any("digest" in key or "trace" in key or "body" in key for key in report)

        audit = app.state.storage.execute(
            """SELECT action,target_id,before_json,after_json FROM audit_log
                 WHERE action='admin.eval.read' ORDER BY created_at DESC,id DESC LIMIT 1"""
        ).fetchone()
        assert audit is not None
        assert audit["target_id"] == target
        assert "PRIVATE" not in str(audit["before_json"] or "") + str(audit["after_json"] or "")


def test_episode_baseline_requires_all_data_read_and_returns_404_for_missing_user(settings) -> None:
    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        storage.ensure_user("local:baseline-target", source="test")
        ordinary = _issue_user_token(storage, "local:baseline-reader", "jrc_baseline_reader")

        denied = client.get(
            _PATH,
            params={"user_id": "local:baseline-target"},
            headers=ordinary,
        )
        assert denied.status_code == 403

        missing = client.get(
            _PATH,
            params={"user_id": "local:missing-baseline-user"},
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert missing.status_code == 404
        assert missing.json()["detail"] == "Пользователь не найден"


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({}, 422),
        ({"user_id": "local:target", "limit": 0}, 422),
        (
            {"user_id": "local:target", "limit": INTERACTION_FAILURE_REPORT_LIMIT + 1},
            422,
        ),
        ({"user_id": "local:target", "since": "2026-08-23T09:00:00Z"}, 422),
        ({"user_id": "local:target", "since": "2026-08-23T12:00:00+03:00"}, 422),
        ({"user_id": "local:target", "since": "2026-02-30T09:00:00+00:00"}, 400),
        ({"user_id": " local:target"}, 400),
        ({"user_id": "local:target", "unknown": "value"}, 400),
    ],
)
def test_episode_baseline_query_contract_is_closed(
    settings,
    params: dict[str, str | int],
    status: int,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        app.state.storage.ensure_user("local:target", source="test")
        response = client.get(
            _PATH,
            params=params,
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert response.status_code == status


def test_episode_baseline_rejects_repeated_query_fields(settings) -> None:
    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        app.state.storage.ensure_user("local:target", source="test")
        response = client.get(
            _PATH,
            params=[("user_id", "local:target"), ("user_id", "local:target")],
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert response.status_code == 400
