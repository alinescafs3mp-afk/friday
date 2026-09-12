"""Relation review is tenant-safe, content-free, bounded and usable from Telegram."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

import friday.storage._graph as storage_graph
from friday.api.kg import _public_relation_candidate_card
from friday.server import create_app
from friday.storage.models import EntityType
from friday.telegram_bridge import TelegramBridge, TelegramConfig

_PRIVATE_EVIDENCE = "PRIVATE-EVIDENCE-MUST-STAY-LOCAL"
_PUBLIC_CARD_KEYS = {
    "id",
    "source_entity_id",
    "target_entity_id",
    "relation_type",
    "status",
    "created_at",
    "reviewed_at",
    "source_name",
    "source_type",
    "target_name",
    "target_type",
    "confidence",
    "evidence",
}


def _headers(storage: Any, user_id: str, preset: str, secret: str) -> dict[str, str]:
    storage.ensure_user(user_id, source="api-token", display_name=user_id, preset_key=preset)
    storage.update_user(user_id, preset_key=preset)
    storage.create_api_token(
        user_id,
        hashlib.sha256(secret.encode()).hexdigest(),
        label="relation-review-test",
        created_by="test",
    )
    return {"Authorization": f"Bearer {secret}"}


def _candidate(app: Any, user_id: str, suffix: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    source = app.state.kg.create_entity(
        user_id,
        f"Synthetic source {suffix}",
        EntityType.PROJECT,
    )
    target = app.state.kg.create_entity(
        user_id,
        f"Synthetic target {suffix}",
        EntityType.CONCEPT,
    )
    candidate = app.state.storage.store_relation_candidate(
        user_id,
        str(source["id"]),
        str(target["id"]),
        "uses",
        confidence=0.82,
        evidence={"excerpt": _PRIVATE_EVIDENCE, "reviewer_hint": "also-private"},
    )
    return source, target, candidate


def _serialized(response: Any) -> str:
    return json.dumps(response.json(), ensure_ascii=False, sort_keys=True)


def test_api_list_and_review_are_tenant_scoped_capability_gated_and_content_free(settings) -> None:
    app = create_app(settings)
    alice_id = "tenant-alice-private-id"
    bob_id = "tenant-bob-private-id"
    guest_id = "tenant-guest-private-id"

    with TestClient(app) as client:
        storage = app.state.storage

        def _sql_row(table: str, row_id: str) -> dict[str, Any]:
            allowed = {
                "relation_candidates": "relation_candidates",
                "entities": "entities",
            }
            row = storage.execute(
                f"SELECT * FROM {allowed[table]} WHERE id=?",
                (row_id,),
            ).fetchone()
            assert row is not None, f"{table} {row_id} missing"
            return dict(row)

        def _audit_ordered() -> list[dict[str, Any]]:
            rows = storage.execute(
                "SELECT id, user_id, action, target_type, target_id, before_json, after_json, "
                "ip_address, request_id, created_at FROM audit_log ORDER BY rowid ASC"
            ).fetchall()
            return [dict(row) for row in rows]

        def _audit_known(row: dict[str, Any]) -> dict[str, Any]:
            return {
                "action": row["action"],
                "user_id": row["user_id"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
            }

        def _assert_audit_delta(
            before: list[dict[str, Any]],
            after: list[dict[str, Any]],
            expected: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            assert after[: len(before)] == before
            delta = after[len(before) :]
            observed = [_audit_known(row) for row in delta]
            assert observed == expected, observed
            return delta

        def _selected_state() -> dict[str, Any]:
            return {
                "alice_lo": _sql_row("relation_candidates", alice["id"]),
                "alice_hi": _sql_row("relation_candidates", alice_hi["id"]),
                "bob": _sql_row("relation_candidates", bob["id"]),
                "guest": _sql_row("relation_candidates", guest["id"]),
                "alice_lo_source": _sql_row("entities", str(alice_source["id"])),
                "alice_lo_target": _sql_row("entities", str(alice_target["id"])),
                "alice_hi_source": _sql_row("entities", str(alice_hi_source["id"])),
                "alice_hi_target": _sql_row("entities", str(alice_hi_target["id"])),
                "bob_source": _sql_row("entities", str(bob_source["id"])),
                "bob_target": _sql_row("entities", str(bob_target["id"])),
                "guest_source": _sql_row("entities", str(guest_source["id"])),
                "guest_target": _sql_row("entities", str(guest_target["id"])),
                "relations": [
                    dict(row)
                    for row in storage.execute("SELECT * FROM relations ORDER BY rowid ASC").fetchall()
                ],
            }

        def _card(
            candidate: dict[str, Any],
            source: dict[str, Any],
            target: dict[str, Any],
            *,
            confidence: float,
        ) -> dict[str, Any]:
            raw = _sql_row("relation_candidates", candidate["id"])
            return {
                "id": raw["id"],
                "source_entity_id": raw["source_entity_id"],
                "target_entity_id": raw["target_entity_id"],
                "relation_type": "uses",
                "status": "suggested",
                "created_at": raw["created_at"],
                "reviewed_at": "",
                "source_name": source["name"],
                "source_type": "project",
                "target_name": target["name"],
                "target_type": "concept",
                "confidence": confidence,
                "evidence": {"present": True, "bytes": 80},
            }

        def _envelope(
            items: list[dict[str, Any]],
            *,
            status: str,
            limit: int,
            offset: int,
            total: int,
        ) -> dict[str, Any]:
            return {
                "items": items,
                "count": len(items),
                "total": total,
                "status": status,
                "limit": limit,
                "offset": offset,
                "matched_at_least": total,
                "truncated": offset + len(items) < total,
            }

        def _private_needles(*extra: str) -> tuple[str, ...]:
            return (
                _PRIVATE_EVIDENCE,
                "also-private",
                "evidence_json",
                "reviewed_by",
                "user_id",
                alice_id,
                bob_id,
                guest_id,
                *extra,
            )

        def _observe_http(call, expected_delta: list[dict[str, Any]]):
            before_state = _selected_state()
            before_audit = _audit_ordered()
            response = call()
            _assert_audit_delta(before_audit, _audit_ordered(), expected_delta)
            assert _selected_state() == before_state
            exposed = _serialized(response)
            for forbidden in _private_needles():
                assert forbidden not in exposed
            return response

        alice_headers = _headers(storage, alice_id, "user", "alice-relations-" + "A" * 32)
        bob_headers = _headers(storage, bob_id, "user", "bob-relations-" + "B" * 32)
        guest_headers = _headers(storage, guest_id, "guest", "guest-relations-" + "G" * 32)
        alice_source, alice_target, alice = _candidate(app, alice_id, "alice")
        alice_hi_source = app.state.kg.create_entity(
            alice_id,
            "Synthetic source alice-hi",
            EntityType.PROJECT,
        )
        alice_hi_target = app.state.kg.create_entity(
            alice_id,
            "Synthetic target alice-hi",
            EntityType.CONCEPT,
        )
        alice_hi = storage.store_relation_candidate(
            alice_id,
            str(alice_hi_source["id"]),
            str(alice_hi_target["id"]),
            "uses",
            confidence=0.91,
            evidence={"excerpt": _PRIVATE_EVIDENCE, "reviewer_hint": "also-private"},
        )
        bob_source, bob_target, bob = _candidate(app, bob_id, "bob")
        guest_source, guest_target, guest = _candidate(app, guest_id, "guest")

        alice_lo_sql = _sql_row("relation_candidates", alice["id"])
        alice_hi_sql = _sql_row("relation_candidates", alice_hi["id"])
        assert alice_lo_sql["confidence"] == 0.82
        assert alice_hi_sql["confidence"] == 0.91
        assert alice_hi_sql["confidence"] > alice_lo_sql["confidence"]
        assert alice_lo_sql["status"] == "suggested"
        assert alice_hi_sql["status"] == "suggested"
        assert alice_lo_sql["reviewed_by"] is None
        assert alice_hi_sql["reviewed_by"] is None
        alice_lo_card = _card(alice, alice_source, alice_target, confidence=0.82)
        alice_hi_card = _card(alice_hi, alice_hi_source, alice_hi_target, confidence=0.91)
        bob_card = _card(bob, bob_source, bob_target, confidence=0.82)
        guest_card = _card(guest, guest_source, guest_target, confidence=0.82)

        alice_page = _observe_http(
            lambda: client.get(
                "/api/kg/relation-candidates",
                params={"status": "suggested", "limit": 1, "offset": 0},
                headers=alice_headers,
            ),
            [],
        )
        assert alice_page.status_code == 200
        assert alice_page.json() == _envelope(
            [alice_hi_card],
            status="suggested",
            limit=1,
            offset=0,
            total=2,
        )
        assert set(alice_page.json()["items"][0]) == _PUBLIC_CARD_KEYS
        assert alice_page.json()["items"][0]["evidence"]["present"] is True

        alice_page2 = _observe_http(
            lambda: client.get(
                "/api/kg/relation-candidates",
                params={"status": "suggested", "limit": 1, "offset": 1},
                headers=alice_headers,
            ),
            [],
        )
        assert alice_page2.status_code == 200
        assert alice_page2.json() == _envelope(
            [alice_lo_card],
            status="suggested",
            limit=1,
            offset=1,
            total=2,
        )

        alice_full = _observe_http(
            lambda: client.get(
                "/api/kg/relation-candidates?status=suggested&limit=5",
                headers=alice_headers,
            ),
            [],
        )
        assert alice_full.status_code == 200
        assert alice_full.json() == _envelope(
            [alice_hi_card, alice_lo_card],
            status="suggested",
            limit=5,
            offset=0,
            total=2,
        )

        bob_page = _observe_http(
            lambda: client.get("/api/kg/relation-candidates", headers=bob_headers),
            [],
        )
        assert bob_page.status_code == 200
        assert bob_page.json() == _envelope(
            [bob_card],
            status="suggested",
            limit=5,
            offset=0,
            total=1,
        )

        cross_tenant = _observe_http(
            lambda: client.post(
                f"/api/kg/relation-candidates/{alice['id']}/review",
                headers=bob_headers,
                json={"status": "accepted"},
            ),
            [],
        )
        assert cross_tenant.status_code == 404
        assert cross_tenant.json() == {"detail": "Кандидат связи не найден"}

        # guest deliberately has kg.read but not kg.write.
        guest_page = _observe_http(
            lambda: client.get("/api/kg/relation-candidates", headers=guest_headers),
            [],
        )
        assert guest_page.status_code == 200
        assert guest_page.json() == _envelope(
            [guest_card],
            status="suggested",
            limit=5,
            offset=0,
            total=1,
        )
        guest_review = _observe_http(
            lambda: client.post(
                f"/api/kg/relation-candidates/{guest['id']}/review",
                headers=guest_headers,
                json={"status": "rejected"},
            ),
            [],
        )
        assert guest_review.status_code == 403
        assert guest_review.json() == {"detail": "Access denied for kg.write (default_deny)"}

        invalid_status = _observe_http(
            lambda: client.get(
                "/api/kg/relation-candidates",
                params={"status": "PRIVATE-STATUS-SENTINEL"},
                headers=alice_headers,
            ),
            [],
        )
        assert invalid_status.status_code == 400
        assert invalid_status.json() == {"detail": "Неизвестный статус кандидата связи"}
        assert "PRIVATE-STATUS-SENTINEL" not in _serialized(invalid_status)

        oversized_offset = _observe_http(
            lambda: client.get(
                "/api/kg/relation-candidates",
                params={"offset": 10**100},
                headers=alice_headers,
            ),
            [],
        )
        assert oversized_offset.status_code == 422
        offset_detail = oversized_offset.json()["detail"][0]
        assert offset_detail["type"] == "less_than_equal"
        assert offset_detail["loc"] == ["query", "offset"]
        assert offset_detail["ctx"] == {"le": 1000000}
        assert offset_detail["input"] == str(10**100)

        oversized_id = "relc_" + "x" * 200
        oversized_review = _observe_http(
            lambda: client.post(
                f"/api/kg/relation-candidates/{oversized_id}/review",
                headers=alice_headers,
                json={"status": "accepted"},
            ),
            [],
        )
        assert oversized_review.status_code == 404
        assert oversized_review.json() == {"detail": "Кандидат связи не найден"}


def test_write_only_grant_cannot_read_or_review_relation_cards(settings) -> None:
    app = create_app(settings)
    user_id = "tenant-write-only"

    with TestClient(app) as client:
        storage = app.state.storage
        headers = _headers(storage, user_id, "guest", "write-only-" + "W" * 32)
        storage.set_permission_override(user_id, "kg.write", "allow")
        storage.set_permission_override(user_id, "kg.read", "deny")
        _source, _target, candidate = _candidate(app, user_id, "write-only")

        assert client.get("/api/kg/relation-candidates", headers=headers).status_code == 403
        response = client.post(
            f"/api/kg/relation-candidates/{candidate['id']}/review",
            headers=headers,
            json={"status": "accepted"},
        )
        assert response.status_code == 403
        raw = storage.execute(
            "SELECT status, reviewed_at, reviewed_by FROM relation_candidates WHERE id=?",
            (candidate["id"],),
        ).fetchone()
        assert raw["status"] == "suggested"
        assert not raw["reviewed_at"]
        assert not raw["reviewed_by"]


def test_review_identity_comes_from_actor_not_request_body(settings) -> None:
    app = create_app(settings)
    user_id = "tenant-review-spoof"
    spoof_reviewer = "SPOOFED-REVIEWER-MUST-NOT-SURVIVE"
    spoof_user = "SPOOFED-USER-MUST-NOT-SURVIVE"

    with TestClient(app) as client:
        storage = app.state.storage
        headers = _headers(storage, user_id, "user", "review-spoof-" + "S" * 32)
        _source, _target, candidate = _candidate(app, user_id, "spoof")
        response = client.post(
            f"/api/kg/relation-candidates/{candidate['id']}/review",
            headers=headers,
            json={
                "status": "rejected",
                "reviewed_by": spoof_reviewer,
                "user_id": spoof_user,
            },
        )
        assert response.status_code == 200
        raw = storage.execute(
            "SELECT reviewed_by FROM relation_candidates WHERE id=?",
            (candidate["id"],),
        ).fetchone()
        assert raw["reviewed_by"] == user_id

        exposed = _serialized(response) + json.dumps(
            storage.list_audit_log(user_id, limit=20),
            ensure_ascii=False,
            default=str,
        )
        assert spoof_reviewer not in exposed
        assert spoof_user not in exposed
        assert _PRIVATE_EVIDENCE not in exposed


def test_review_is_idempotent_but_terminal_and_audit_is_content_free(settings) -> None:
    app = create_app(settings)
    user_id = "tenant-terminal-private-id"

    with TestClient(app) as client:
        storage = app.state.storage

        def _sql_row(table: str, row_id: str) -> dict[str, Any]:
            allowed = {
                "relation_candidates": "relation_candidates",
                "entities": "entities",
                "relations": "relations",
            }
            row = storage.execute(
                f"SELECT * FROM {allowed[table]} WHERE id=?",
                (row_id,),
            ).fetchone()
            assert row is not None, f"{table} {row_id} missing"
            return dict(row)

        def _audit_ordered() -> list[dict[str, Any]]:
            rows = storage.execute(
                "SELECT id, user_id, action, target_type, target_id, before_json, after_json, "
                "ip_address, request_id, created_at FROM audit_log ORDER BY rowid ASC"
            ).fetchall()
            return [dict(row) for row in rows]

        def _audit_known(row: dict[str, Any]) -> dict[str, Any]:
            return {
                "action": row["action"],
                "user_id": row["user_id"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
            }

        def _assert_audit_delta(
            before: list[dict[str, Any]],
            after: list[dict[str, Any]],
            expected: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            assert after[: len(before)] == before
            delta = after[len(before) :]
            observed = [_audit_known(row) for row in delta]
            assert observed == expected, observed
            return delta

        def _edges() -> list[dict[str, Any]]:
            return [
                dict(row)
                for row in storage.execute(
                    "SELECT * FROM relations WHERE user_id=? ORDER BY rowid ASC",
                    (user_id,),
                ).fetchall()
            ]

        def _candidate_sql() -> dict[str, Any]:
            return _sql_row("relation_candidates", candidate["id"])

        def _accepted_after(sql: dict[str, Any]) -> dict[str, Any]:
            return {
                "confidence": 0.82,
                "created_at": sql["created_at"],
                "id": sql["id"],
                "private_fields_count": 1,
                "private_items_count": 2,
                "relation_type": "uses",
                "reviewed_at": sql["reviewed_at"],
                "source_entity_id": sql["source_entity_id"],
                "status": "accepted",
                "target_entity_id": sql["target_entity_id"],
            }

        def _public_item(sql: dict[str, Any]) -> dict[str, Any]:
            return {
                "id": sql["id"],
                "source_entity_id": sql["source_entity_id"],
                "target_entity_id": sql["target_entity_id"],
                "relation_type": "uses",
                "status": "accepted",
                "created_at": sql["created_at"],
                "reviewed_at": sql["reviewed_at"],
                "source_name": source["name"],
                "source_type": "project",
                "target_name": target["name"],
                "target_type": "concept",
                "confidence": 0.82,
                "evidence": {"present": True, "bytes": 80},
            }

        def _private_scan(payload: str) -> None:
            for forbidden in (
                _PRIVATE_EVIDENCE,
                "also-private",
                str(source["name"]),
                str(target["name"]),
                "evidence_json",
                "reviewed_by",
                user_id,
            ):
                assert forbidden not in payload

        headers = _headers(storage, user_id, "user", "terminal-relations-" + "T" * 32)
        source, target, candidate = _candidate(app, user_id, "terminal")
        path = f"/api/kg/relation-candidates/{candidate['id']}/review"
        accepted_delta = [
            {
                "action": "relation_candidate.accepted",
                "user_id": user_id,
                "target_type": "relation_candidate",
                "target_id": candidate["id"],
            }
        ]

        before_first_audit = _audit_ordered()
        first = client.post(path, headers=headers, json={"status": "accepted"})
        first_delta = _assert_audit_delta(before_first_audit, _audit_ordered(), accepted_delta)
        assert first.status_code == 200
        sql_after_first = _candidate_sql()
        assert sql_after_first["status"] == "accepted"
        assert sql_after_first["reviewed_by"] == user_id
        assert sql_after_first["reviewed_at"]
        assert sql_after_first["source_entity_id"] == str(source["id"])
        assert sql_after_first["target_entity_id"] == str(target["id"])
        assert sql_after_first["relation_type"] == "uses"
        assert sql_after_first["user_id"] == user_id
        assert sql_after_first["confidence"] == 0.82
        expected_item = _public_item(sql_after_first)
        assert first.json() == {"status": "accepted", "item": expected_item}
        assert set(first.json()["item"]) == _PUBLIC_CARD_KEYS
        assert _PRIVATE_EVIDENCE not in _serialized(first)
        for forbidden in (_PRIVATE_EVIDENCE, "also-private", "evidence_json", "reviewed_by", user_id):
            assert forbidden not in _serialized(first)
        edges = _edges()
        assert len(edges) == 1
        edge = edges[0]
        assert edge["source_entity_id"] == str(source["id"])
        assert edge["target_entity_id"] == str(target["id"])
        assert edge["relation_type"] == "uses"
        assert edge["weight"] == 0.82
        assert edge["user_id"] == user_id
        assert edge["deleted_at"] is None
        assert edge["valid_from"] == ""
        assert json.loads(str(first_delta[0]["after_json"] or "{}")) == _accepted_after(sql_after_first)
        assert first_delta[0]["before_json"] is None
        _private_scan(str(first_delta[0]["after_json"] or ""))

        before_replay_audit = _audit_ordered()
        repeated = client.post(path, headers=headers, json={"status": "accepted"})
        replay_delta = _assert_audit_delta(before_replay_audit, _audit_ordered(), accepted_delta)
        assert repeated.status_code == 200
        sql_after_replay = _candidate_sql()
        assert sql_after_replay == sql_after_first
        replay_edges = _edges()
        assert replay_edges == edges
        assert repeated.json() == {"status": "accepted", "item": expected_item}
        assert json.loads(str(replay_delta[0]["after_json"] or "{}")) == _accepted_after(sql_after_replay)
        assert replay_delta[0]["before_json"] is None
        _private_scan(str(replay_delta[0]["after_json"] or ""))

        before_opposite_audit = _audit_ordered()
        opposite = client.post(path, headers=headers, json={"status": "rejected"})
        _assert_audit_delta(before_opposite_audit, _audit_ordered(), [])
        assert opposite.status_code == 409
        assert opposite.json() == {"detail": "Кандидат связи уже решён другим образом"}
        assert _candidate_sql() == sql_after_first
        assert _edges() == edges
        assert "PRIVATE-EVIDENCE-MUST-STAY-LOCAL" not in _serialized(opposite)

        before_suggested_audit = _audit_ordered()
        decided_page = client.get("/api/kg/relation-candidates", headers=headers)
        _assert_audit_delta(before_suggested_audit, _audit_ordered(), [])
        assert decided_page.status_code == 200
        assert decided_page.json() == {
            "items": [],
            "count": 0,
            "total": 0,
            "status": "suggested",
            "limit": 5,
            "offset": 0,
            "matched_at_least": 0,
            "truncated": False,
        }

        before_accepted_get = _audit_ordered()
        accepted_page = client.get(
            "/api/kg/relation-candidates",
            params={"status": "accepted"},
            headers=headers,
        )
        _assert_audit_delta(before_accepted_get, _audit_ordered(), [])
        assert accepted_page.status_code == 200
        assert accepted_page.json() == {
            "items": [expected_item],
            "count": 1,
            "total": 1,
            "status": "accepted",
            "limit": 5,
            "offset": 0,
            "matched_at_least": 1,
            "truncated": False,
        }
        assert accepted_page.json()["items"][0] == first.json()["item"]
        for forbidden in (_PRIVATE_EVIDENCE, "also-private", "evidence_json", "reviewed_by", user_id):
            assert forbidden not in _serialized(accepted_page)
        assert _candidate_sql() == sql_after_first
        assert _edges() == edges
        assert len(_audit_ordered()) == len(before_first_audit) + 2


def test_committed_review_never_depends_on_a_postcommit_endpoint_lookup(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A committed decision must still return and audit from its transaction snapshot."""

    app = create_app(settings)
    user_id = "tenant-postcommit-race"
    original_lookup = storage_graph._bounded_relation_candidate_by_id
    postcommit_called = False

    with TestClient(app) as client:
        storage = app.state.storage
        headers = _headers(storage, user_id, "user", "postcommit-race-" + "P" * 32)
        _source, target, candidate = _candidate(app, user_id, "postcommit")

        def hide_in_forbidden_gap(instance: Any, uid: str, candidate_id: str) -> Any:
            nonlocal postcommit_called
            postcommit_called = True
            storage.soft_delete_entity(str(target["id"]), user_id)
            return original_lookup(instance, uid, candidate_id)

        # The API module already holds its own imported preflight reference.  This
        # hook therefore catches only the old storage-level lookup after COMMIT.
        monkeypatch.setattr(
            storage_graph,
            "_bounded_relation_candidate_by_id",
            hide_in_forbidden_gap,
        )
        response = client.post(
            f"/api/kg/relation-candidates/{candidate['id']}/review",
            headers=headers,
            json={"status": "accepted"},
        )

        assert response.status_code == 200
        assert postcommit_called is False
        raw = storage.execute(
            "SELECT status FROM relation_candidates WHERE id=?", (candidate["id"],)
        ).fetchone()
        assert raw["status"] == "accepted"
        edge_count = storage.execute(
            "SELECT COUNT(*) AS count FROM relations WHERE user_id=?", (user_id,)
        ).fetchone()
        assert edge_count["count"] == 1
        audit_count = sum(
            row["action"] == "relation_candidate.accepted"
            for row in storage.list_audit_log(user_id, limit=20)
        )
        assert audit_count == 1


def test_rejected_decision_keeps_its_content_free_audit_action(settings) -> None:
    app = create_app(settings)
    user_id = "tenant-rejected-audit"

    with TestClient(app) as client:
        storage = app.state.storage
        headers = _headers(storage, user_id, "user", "rejected-audit-" + "Q" * 32)
        source, target, candidate = _candidate(app, user_id, "rejected-audit")
        response = client.post(
            f"/api/kg/relation-candidates/{candidate['id']}/review",
            headers=headers,
            json={"status": "rejected"},
        )
        assert response.status_code == 200

        rows = [
            row
            for row in storage.list_audit_log(user_id, limit=20)
            if row["action"] == "relation_candidate.rejected"
        ]
        assert len(rows) == 1
        ledger_text = str(rows[0]["after_json"] or "")
        for forbidden in (
            _PRIVATE_EVIDENCE,
            str(source["name"]),
            str(target["name"]),
            "evidence_json",
            "reviewed_by",
            user_id,
        ):
            assert forbidden not in ledger_text


@pytest.mark.parametrize("decision", ["accepted", "rejected"])
def test_storage_authority_closes_the_route_precheck_race(
    settings,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    """An endpoint dying after the route read cannot be reviewed by guessed id."""

    app = create_app(settings)
    user_id = f"tenant-race-{decision}"
    with TestClient(app) as client:
        storage = app.state.storage
        headers = _headers(storage, user_id, "user", f"race-{decision}-" + "R" * 32)
        _source, target, candidate = _candidate(app, user_id, decision)
        original_review = app.state.kg.review_relation_candidate

        def remove_endpoint_then_review(*args: Any, **kwargs: Any) -> Any:
            storage.soft_delete_entity(str(target["id"]), user_id)
            return original_review(*args, **kwargs)

        monkeypatch.setattr(app.state.kg, "review_relation_candidate", remove_endpoint_then_review)
        response = client.post(
            f"/api/kg/relation-candidates/{candidate['id']}/review",
            headers=headers,
            json={"status": decision},
        )
        assert response.status_code == 404

        raw = storage.execute(
            "SELECT status, reviewed_at, reviewed_by FROM relation_candidates WHERE id=?",
            (candidate["id"],),
        ).fetchone()
        assert raw["status"] == "suggested"
        assert not raw["reviewed_at"]
        assert not raw["reviewed_by"]
        relation_count = storage.execute(
            "SELECT COUNT(*) AS count FROM relations WHERE user_id=?",
            (user_id,),
        ).fetchone()
        assert relation_count["count"] == 0


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf")])
def test_non_finite_confidence_is_projected_as_zero(non_finite: float) -> None:
    # SQLite's constraints reject these values. The projection still defends
    # import/migration rows and non-SQL callers instead of serialising invalid
    # JSON numbers.
    card = _public_relation_candidate_card({"confidence": non_finite})
    assert card["confidence"] == 0.0


class _FakeResponse:
    def __init__(self, payload: Any = None, *, status_code: int = 200) -> None:
        self._payload = payload if payload is not None else {"ok": True}
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.text = json.dumps(self._payload, ensure_ascii=False)

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeTelegramClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, json: dict[str, Any] | None = None, **_kwargs: Any) -> _FakeResponse:
        self.calls.append((url, json or {}))
        return _FakeResponse({"ok": True, "result": {}})


class _FakeBackendClient:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeResponse:
        parsed = urlsplit(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        body = json.loads(content.decode()) if content else None
        self.calls.append({"method": method, "path": path, "body": body, "headers": headers or {}})
        payload = self.responses.get(path, self.responses.get(parsed.path, {}))
        return _FakeResponse(payload)


def _bridge(tmp_path: Any) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "relations-telegram.sqlite3"),
        )
    )


def _relation_item(index: int) -> dict[str, Any]:
    return {
        "id": f"relc_{index:016d}",
        "source_name": f"Synthetic source {index}",
        "source_type": "project",
        "target_name": f"Synthetic target {index}",
        "target_type": "concept",
        "relation_type": "uses",
        "confidence": 0.75,
        "evidence_json": _PRIVATE_EVIDENCE,
        "reviewed_by": "private-reviewer",
    }


@pytest.mark.asyncio
async def test_relations_command_is_bounded_markup_safe_and_buttons_belong_to_invoker(tmp_path) -> None:
    # Exercise the longest decimal identity SQLite can persist.  The previous
    # twenty-digit fixture overflowed signed INTEGER before the callback
    # boundary under test was reached.
    invoker = (1 << 63) - 1
    items = [_relation_item(index) for index in range(1, 7)]
    items[0].update(
        {
            "source_name": (
                "Source\n[click](https://trace.invalid) plain.example.invalid "
                "mail@example.invalid\u202e spoof"
            ),
            "source_type": "[raw-type](https://type.invalid)",
            "relation_type": "[raw-relation](https://relation.invalid)",
            "confidence": float("nan"),
        }
    )
    items[1].update({"relation_type": "same_as", "confidence": float("inf")})
    bridge = _bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/kg/relation-candidates?status=suggested&limit=5": {
                "items": items,
                "total": 192,
            }
        }
    )
    update = {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "chat": {"id": 5001, "type": "supergroup"},
            "from": {"id": invoker, "first_name": "Synthetic"},
            "text": "/relations",
        },
    }
    try:
        await bridge._process_update(telegram, backend, update, cached_response=None)

        assert backend.calls[0]["method"] == "GET"
        assert backend.calls[0]["path"] == "/api/kg/relation-candidates?status=suggested&limit=5"
        sends = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        assert len(sends) == 6, "one intro plus exactly five bounded cards expected"
        assert "5" in sends[0]["text"] and "192" in sends[0]["text"]
        telegram_text = json.dumps(telegram.calls, ensure_ascii=False)
        assert "Synthetic source 6" not in telegram_text
        for forbidden in (
            _PRIVATE_EVIDENCE,
            "private-reviewer",
            "raw-type",
            "raw-relation",
            "\u202e",
        ):
            assert forbidden not in telegram_text
        assert "<a href" not in sends[1]["text"]
        assert "<code>" in sends[1]["text"]
        assert "связь —" in sends[1]["text"]
        assert "↔" in sends[2]["text"], "same_as was misleadingly rendered as directional"
        assert "Типы объектов: проект ↔ понятие" in sends[2]["text"]
        assert "plain.example.invalid" in sends[1]["text"]
        assert "mail@example.invalid" in sends[1]["text"]
        assert "0%" in sends[1]["text"] and "0%" in sends[2]["text"]

        cards = [payload for payload in sends if payload.get("reply_markup")]
        assert len(cards) == 5
        callback_values = [
            button["callback_data"]
            for card in cards
            for row in card["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        assert len(callback_values) == 10
        assert all(len(value.encode()) <= 64 for value in callback_values)
        assert all(value.rsplit(".", 1)[-1] == str(invoker) for value in callback_values)

        selected = callback_values[0]
        original_markup = cards[0]["reply_markup"]
        calls_before_wrong_user = len(backend.calls)
        await bridge._process_callback_query(
            telegram,
            backend,
            {
                "id": "wrong-user",
                "from": {"id": invoker - 1},
                "data": selected,
                "message": {
                    "message_id": 11,
                    "chat": {"id": 5001, "type": "supergroup"},
                    "reply_markup": original_markup,
                },
            },
        )
        assert len(backend.calls) == calls_before_wrong_user
        wrong_ack = [
            payload
            for url, payload in telegram.calls
            if url.endswith("/answerCallbackQuery") and payload.get("callback_query_id") == "wrong-user"
        ]
        assert wrong_ack and wrong_ack[-1].get("show_alert") is True

        await bridge._process_callback_query(
            telegram,
            backend,
            {
                "id": "right-user",
                "from": {"id": invoker},
                "data": selected,
                "message": {
                    "message_id": 11,
                    "chat": {"id": 5001, "type": "supergroup"},
                    "reply_markup": original_markup,
                },
            },
        )
        decisions = [call for call in backend.calls if call["method"] == "POST"]
        assert len(decisions) == 1
        assert decisions[0]["path"] == "/api/kg/relation-candidates/relc_0000000000000001/review"
        assert decisions[0]["body"]["status"] == "accepted"
        assert decisions[0]["body"]["telegram_user"]["id"] == invoker
        assert any(url.endswith("/editMessageReplyMarkup") for url, _payload in telegram.calls)
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_relations_command_never_sends_an_oversized_callback(tmp_path) -> None:
    item = _relation_item(1)
    item["id"] = "relc_" + "x" * 80
    bridge = _bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/kg/relation-candidates?status=suggested&limit=5": {
                "items": [item],
                "total": 1,
            }
        }
    )
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 3,
                "message": {
                    "message_id": 13,
                    "chat": {"id": 5001, "type": "supergroup"},
                    "from": {"id": (1 << 63) - 1},
                    "text": "/relations",
                },
            },
            cached_response=None,
        )
        sends = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        assert len(sends) == 2
        assert "Кнопки недоступны" in sends[-1]["text"]
        assert "reply_markup" not in sends[-1]
        assert "callback_data" not in json.dumps(sends, ensure_ascii=False)
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_help_and_status_point_to_relation_review(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/me": {"actor": {"preset_key": "user"}}})
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 2,
                "message": {
                    "message_id": 12,
                    "chat": {"id": 5001},
                    "from": {"id": 1001},
                    "text": "/help",
                },
            },
            cached_response=None,
        )
        help_text = " ".join(
            str(payload.get("text") or "") for url, payload in telegram.calls if url.endswith("/sendMessage")
        )
        assert "/relations" in help_text
        status_text = bridge._format_status("work", {"pending_relation_candidates": 3})
        assert "3" in status_text and "/relations" in status_text
        assert "/relations" not in bridge._format_status("work", {"pending_relation_candidates": 0})
    finally:
        bridge._inbox.close()
