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

        import re
        import sqlite3
        from contextlib import closing
        from datetime import UTC, datetime

        foreign = "local:baseline-foreign"
        assert foreign != target
        foreign_conversation_id, foreign_retained = _seed_private_failure(app.state.storage, foreign)
        since = "2020-01-01T00:00:00+00:00"
        owner_id = "964e5f17-a4bf-5744-a5c6-b7bfbdcd7bf0"
        expected_report = {
            "schema": "friday.interaction-episode-baseline.v1",
            "observed_turns": 1,
            "observed_episodes": 1,
            "assistant_committed": 0,
            "precommit_failures": 1,
            "assistant_commit_rate_milli": 0,
            "intent": {"document_work": 1},
            "completion": {"failed": 1},
            "publication": {"not_attempted": 1},
            "signals": {
                "ambiguity_present": 0,
                "partial_coverage": 0,
                "state_restored": 0,
                "authority_rechecked": 0,
            },
            "failure_stages": {"capability": 1},
            "failure_reasons": {"internal_error": 1},
            "precommit_routes": {"archive_read": 1},
            "bounded": False,
        }
        audit_private_values = (
            "PRIVATE BASELINE TITLE 4821",
            "PRIVATE TURN IDENTIFIER 4821",
            "PRIVATE FAILURE BODY 4821",
            foreign,
            conversation_id,
            retained["turn_digest"],
            retained["conversation_digest"],
            retained["trace_json"],
            foreign_conversation_id,
            foreign_retained["turn_digest"],
            foreign_retained["conversation_digest"],
            foreign_retained["trace_json"],
        )

        def canonical(value):
            return json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            )

        def selected_state():
            with closing(sqlite3.connect(settings.database_path.as_uri() + "?mode=ro", uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                business = {
                    table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
                    for table in ("messages", "interaction_failure_traces", "conversations")
                }
                audits = [dict(row) for row in conn.execute("SELECT rowid, * FROM audit_log ORDER BY rowid")]
                return business, audits

        expected_state, audit_prefix = selected_state()

        def assert_read_effects(read_response, window, expected_limit):
            nonlocal audit_prefix
            actual_state, actual_audits = selected_state()
            assert actual_state == expected_state
            assert len(actual_audits) == len(audit_prefix) + 1
            assert actual_audits[:-1] == audit_prefix
            row = actual_audits[-1]
            assert re.fullmatch(r"audit_[0-9a-f]{16}", row["id"])
            assert row["id"] not in {old["id"] for old in audit_prefix}
            request_id = read_response.headers["x-request-id"]
            assert re.fullmatch(r"[0-9a-f]{24}", request_id)
            assert request_id not in {old["request_id"] for old in audit_prefix}
            created_at = row["created_at"]
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000000\+00:00", created_at)
            parsed = datetime.fromisoformat(created_at)
            assert parsed.isoformat(timespec="microseconds") == created_at
            assert window[0].replace(microsecond=0) <= parsed <= window[1]
            assert row == {
                "rowid": audit_prefix[-1]["rowid"] + 1 if audit_prefix else 1,
                "id": row["id"],
                "user_id": owner_id,
                "action": "admin.eval.read",
                "target_type": "user",
                "target_id": target,
                "before_json": None,
                "after_json": json.dumps(
                    {"limit": expected_limit, "since": since}, ensure_ascii=False, sort_keys=True
                ),
                "ip_address": "",
                "request_id": request_id,
                "created_at": created_at,
            }
            appended = "\n".join(
                str(value or "")
                for audit_row in actual_audits[len(audit_prefix) :]
                for value in audit_row.values()
            )
            for private_value in audit_private_values:
                assert private_value not in appended
            audit_prefix = actual_audits

        request_started = datetime.now(UTC)
        response = client.get(
            _PATH,
            params={
                "user_id": target,
                "since": "2020-01-01T00:00:00+00:00",
                "limit": 7,
            },
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        request_finished = datetime.now(UTC)

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
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
        expected_payload = {"user_id": target, "since": since, "limit": 7, "report": expected_report}
        assert canonical(payload) == canonical(expected_payload)

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
        assert_read_effects(response, (request_started, request_finished), 7)

        limited_started = datetime.now(UTC)
        limited = client.get(
            _PATH,
            params={"user_id": target, "since": since, "limit": 1},
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        limited_finished = datetime.now(UTC)
        assert limited.status_code == 200
        assert limited.headers["content-type"] == "application/json"
        limited_expected = {
            "user_id": target,
            "since": since,
            "limit": 1,
            "report": {**expected_report, "bounded": True},
        }
        assert canonical(limited.json()) == canonical(limited_expected)
        limited_serialized = canonical(limited.json())
        for private_value in audit_private_values:
            assert private_value not in limited_serialized
        assert_read_effects(limited, (limited_started, limited_finished), 1)


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
