"""Conflicts and merges are review queues; the owner must be able to clear them in chat.

G4: 200 suggested conflicts and 20 merge candidates on the live install, with
zero Telegram path for conflicts (merges already had /merges). The cheapest
path is the same as always: a capability-gated tool plus a bridge command that
shows a portion and never re-shows a decided row.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.server import create_app
from friday.storage.models import KnowledgeObject, RawObject, new_id
from friday.web_surfer import WebSurfer
from tests.conftest import run_with_approval


def _knowledge(storage, user_id: str, title: str, content: str) -> str:
    raw = storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id=user_id,
            source="test",
            source_ref=new_id("src"),
            raw_content=content,
            content_type="text",
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            received_at=datetime.now(UTC).isoformat(),
        )
    )
    return storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id=user_id,
            raw_object_id=raw.id,
            title=title,
            summary=content[:120],
            content=content,
            knowledge_kind="note",
            importance=0.5,
            created_at=datetime.now(UTC).isoformat(),
        )
    ).id


def _seed_conflict(storage, user_id: str) -> str:
    a = _knowledge(storage, user_id, "Первая редакция", "Текст A про объект.")
    b = _knowledge(storage, user_id, "Вторая редакция", "Текст B про объект.")
    row = storage.store_knowledge_conflict(
        user_id,
        a,
        b,
        conflict_type="near_duplicate",
        confidence=0.9,
        evidence={"method": "test"},
    )
    return str(row["id"])


def test_http_conflict_decide_dismisses_and_hides_from_suggested(settings):
    import json
    import re
    from dataclasses import replace
    from datetime import timedelta

    import friday.storage._knowledge as knowledge_storage
    from friday.permissions import LEGACY_OWNER_USER_ID
    from friday.storage.models import AuditEntry

    person_p = "person-reviewer-p"
    person_d = "person-denied-d"
    foreign_tenant = "tenant-foreign-decide"
    secret_p = "scoped-p-secret-" + "P" * 32
    secret_d = "scoped-d-secret-" + "D" * 32
    secret_foreign = "foreign-secret-" + "F" * 32
    spoof_user = "spoof-user-id-canary"
    spoof_reviewer = "spoof-reviewed-by-canary"
    spoof_status = "spoof-status-canary"
    spoof_note = "spoof-resolution-note-canary"
    missing_id = "conf_0123456789abcdef"
    pinned_t = "2026-09-08T22:40:00+00:00"
    triage = {
        "hint": "likely_different_records",
        "label_ru": "внимание: разные записи?",
        "jaccard": 0.6667,
        "length_ratio": 1.0,
        "data_diff_share": 1.0,
    }
    audit_id_re = re.compile(r"^audit_[0-9a-f]{16}$")
    conf_id_re = re.compile(r"^conf_[0-9a-f]{16}$")
    ko_id_re = re.compile(r"^ko_[0-9a-f]{16}$")
    assert person_p != LEGACY_OWNER_USER_ID
    assert person_d != person_p
    assert isinstance(settings.api_token, str) and settings.api_token

    def _headers(user_id: str, secret: str) -> dict[str, str]:
        storage.ensure_user(user_id, preset_key="user")
        storage.create_api_token(
            user_id,
            hashlib.sha256(secret.encode()).hexdigest(),
            label="conflict-decide-http",
            created_by="test",
        )
        return {"Authorization": f"Bearer {secret}"}

    def _rows(table: str) -> list[dict]:
        allowed = {
            "raw_objects": "raw_objects",
            "knowledge_objects": "knowledge_objects",
            "knowledge_object_versions": "knowledge_object_versions",
            "knowledge_conflicts": "knowledge_conflicts",
            "api_tokens": "api_tokens",
        }
        found = storage.execute(f"SELECT * FROM {allowed[table]} ORDER BY rowid ASC").fetchall()
        return [dict(row) for row in found]

    def _business() -> dict:
        tokens = []
        for row in _rows("api_tokens"):
            tokens.append({key: value for key, value in row.items() if key != "last_used_at"})
        return {
            "raw_objects": _rows("raw_objects"),
            "knowledge_objects": _rows("knowledge_objects"),
            "knowledge_object_versions": _rows("knowledge_object_versions"),
            "knowledge_conflicts": _rows("knowledge_conflicts"),
            "api_tokens": tokens,
        }

    def _audit() -> list[dict]:
        found = storage.execute(
            "SELECT rowid, id, user_id, action, target_type, target_id, before_json, "
            "after_json, ip_address, request_id, created_at FROM audit_log ORDER BY rowid ASC"
        ).fetchall()
        return [dict(row) for row in found]

    def _decode(value):
        if value in (None, ""):
            return None
        if isinstance(value, (dict, list)):
            return value
        return json.loads(value)

    def _conflict(row_id: str) -> dict:
        row = storage.execute("SELECT * FROM knowledge_conflicts WHERE id=?", (row_id,)).fetchone()
        assert row is not None, row_id
        return dict(row)

    def _ko(row_id: str) -> dict:
        row = storage.execute("SELECT * FROM knowledge_objects WHERE id=?", (row_id,)).fetchone()
        assert row is not None, row_id
        return dict(row)

    def _aware_created(value) -> str:
        assert value not in (None, "")
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        assert parsed.tzinfo is not None and parsed.utcoffset() is not None
        return str(value)

    def _card(conflict_row: dict, *, status: str, reviewed_at: str, note_chars: int) -> dict:
        left = _ko(conflict_row["knowledge_a_id"])
        right = _ko(conflict_row["knowledge_b_id"])
        assert conf_id_re.fullmatch(str(conflict_row["id"]))
        assert ko_id_re.fullmatch(str(left["id"]))
        assert ko_id_re.fullmatch(str(right["id"]))
        assert left["id"] == conflict_row["knowledge_a_id"]
        assert right["id"] == conflict_row["knowledge_b_id"]
        assert left["title"] == "Первая редакция"
        assert left["summary"] == "Текст A про объект."
        assert left["lifecycle_stage"] == "active"
        assert right["title"] == "Вторая редакция"
        assert right["summary"] == "Текст B про объект."
        assert right["lifecycle_stage"] == "active"
        return {
            "id": conflict_row["id"],
            "knowledge_a_id": conflict_row["knowledge_a_id"],
            "knowledge_b_id": conflict_row["knowledge_b_id"],
            "conflict_type": "near_duplicate",
            "confidence": 0.9,
            "status": status,
            "created_at": _aware_created(conflict_row["created_at"]),
            "reviewed_at": reviewed_at,
            "knowledge_a_title": "Первая редакция",
            "knowledge_a_summary": "Текст A про объект.",
            "knowledge_a_stage": "active",
            "knowledge_a_superseded_by": "",
            "knowledge_b_title": "Вторая редакция",
            "knowledge_b_summary": "Текст B про объект.",
            "knowledge_b_stage": "active",
            "knowledge_b_superseded_by": "",
            "evidence": {"present": True, "bytes": 18},
            "resolution_note_chars": note_chars,
        }

    def _envelope(items, *, status: str, total: int) -> dict:
        return {
            "items": items,
            "count": len(items),
            "total": total,
            "status": status,
            "matched_at_least": total,
            "truncated": False,
        }

    def _needles(*extra: str) -> tuple[str, ...]:
        return (
            secret_p,
            secret_d,
            secret_foreign,
            settings.api_token,
            spoof_user,
            spoof_reviewer,
            spoof_status,
            spoof_note,
            foreign_id,
            foreign_a,
            foreign_b,
            canary_raw,
            canary_ref,
            canary_ko,
            canary_raw_content,
            "CANARY-FOREIGN-TITLE",
            *extra,
        )

    def _scan(payload, *extra: str) -> None:
        text = None
        decoded = payload
        if (
            not isinstance(payload, (str, dict, list))
            and hasattr(payload, "status_code")
            and hasattr(payload, "text")
        ):
            decoded = payload.json()
            text = payload.text
        blob = decoded if isinstance(decoded, str) else json.dumps(decoded, ensure_ascii=False, default=str)
        for needle in _needles(*extra):
            assert needle not in blob, needle
            if text is not None:
                assert needle not in text, needle

    def _observe(call, *, audit_delta=0, business=None):
        before_business = _business() if business is None else business
        before_audit = _audit()
        response = call()
        after_audit = _audit()
        assert after_audit[: len(before_audit)] == before_audit
        delta = after_audit[len(before_audit) :]
        assert len(delta) == audit_delta, [row["action"] for row in delta]
        assert _business() == before_business
        return response, before_audit, after_audit, delta

    with TestClient(create_app(replace(settings, shared_archive=True))) as client:
        storage = client.app.state.storage
        headers_p = _headers(person_p, secret_p)
        headers_d = _headers(person_d, secret_d)
        storage.set_permission_override(person_d, "knowledge.edit", "deny")
        storage.ensure_user(foreign_tenant, preset_key="user")
        storage.create_api_token(
            foreign_tenant,
            hashlib.sha256(secret_foreign.encode()).hexdigest(),
            label="foreign-decide",
            created_by="test",
        )

        target_id = _seed_conflict(storage, LEGACY_OWNER_USER_ID)
        confirmed_id = _seed_conflict(storage, LEGACY_OWNER_USER_ID)
        storage.review_knowledge_conflict(
            LEGACY_OWNER_USER_ID,
            confirmed_id,
            "confirmed",
            reviewed_by=LEGACY_OWNER_USER_ID,
            resolution_note="prior confirmed non-target",
        )
        foreign_id = _seed_conflict(storage, foreign_tenant)
        canary_ko = _knowledge(storage, foreign_tenant, "CANARY-FOREIGN-TITLE", "CANARY-FOREIGN-RAW")
        canary_row = _ko(canary_ko)
        canary_raw = str(canary_row["raw_object_id"])
        raw_row = storage.execute("SELECT * FROM raw_objects WHERE id=?", (canary_raw,)).fetchone()
        raw_stored = dict(raw_row)
        canary_ref = str(raw_stored["source_ref"])
        canary_raw_content = str(raw_stored["raw_content"])
        assert canary_raw_content == "CANARY-FOREIGN-RAW"
        foreign_sql = _conflict(foreign_id)
        foreign_a = foreign_sql["knowledge_a_id"]
        foreign_b = foreign_sql["knowledge_b_id"]

        storage.log_audit(
            AuditEntry(
                id=new_id("audit"),
                user_id=LEGACY_OWNER_USER_ID,
                action="inbox.classify",
                target_type="inbox",
                target_id="inbox_prior_nonvacuous",
                after_json={"status": "classified"},
            )
        )

        target_sql = _conflict(target_id)
        target_a = target_sql["knowledge_a_id"]
        target_b = target_sql["knowledge_b_id"]
        created_at = _aware_created(target_sql["created_at"])
        assert target_sql["user_id"] == LEGACY_OWNER_USER_ID
        assert target_sql["conflict_type"] == "near_duplicate"
        assert float(target_sql["confidence"]) == 0.9
        assert target_sql["status"] == "suggested"
        assert target_sql["evidence_json"] == '{"method": "test"}'
        assert conf_id_re.fullmatch(str(target_id))
        assert ko_id_re.fullmatch(str(target_a))
        assert ko_id_re.fullmatch(str(target_b))
        assert _ko(target_a)["title"] == "Первая редакция"
        assert _ko(target_b)["title"] == "Вторая редакция"
        assert _conflict(confirmed_id)["status"] == "confirmed"
        assert _conflict(foreign_id)["user_id"] == foreign_tenant
        closed = _card(target_sql, status="suggested", reviewed_at="", note_chars=0)
        assert len(closed) == 18
        suggested_item = dict(closed)
        suggested_item["triage"] = dict(triage)
        dismissed_card = _card(target_sql, status="dismissed", reviewed_at=pinned_t, note_chars=15)
        path = f"/api/kg/conflicts/{target_id}/decide"
        assert len(path) == 46

        setup_business = _business()
        setup_audit = _audit()
        assert setup_audit, "prior audit row required"

        listed, _, _, listed_delta = _observe(
            lambda: client.get("/api/kg/conflicts?status=suggested", headers=headers_p),
            audit_delta=0,
            business=setup_business,
        )
        assert listed.status_code == 200, listed.text
        assert "limit" not in listed.json() and "offset" not in listed.json()
        assert listed.json() == _envelope([suggested_item], status="suggested", total=1)
        _scan(listed)

        real_utc = knowledge_storage.utc_now
        t0 = datetime.now(UTC)
        knowledge_storage.utc_now = lambda: pinned_t
        try:
            before_dismiss_business = _business()
            before_dismiss_audit = _audit()
            decided = client.post(
                path,
                json={
                    "decision": "dismiss",
                    "user_id": spoof_user,
                    "reviewed_by": spoof_reviewer,
                    "status": spoof_status,
                    "resolution_note": spoof_note,
                },
                headers=headers_p,
            )
        finally:
            knowledge_storage.utc_now = real_utc
        t1 = datetime.now(UTC)
        after_dismiss_audit = _audit()
        after_dismiss_business = _business()
        assert decided.status_code == 200, decided.text
        assert decided.json() == {"status": "dismissed", "item": dismissed_card}
        assert "winner_id" not in decided.json()
        assert "conflict" not in decided.json()
        assert "triage" not in decided.json()["item"]
        assert len(decided.json()["item"]) == 18
        _scan(decided)

        expected_sql = dict(target_sql)
        expected_sql["status"] = "dismissed"
        expected_sql["reviewed_at"] = pinned_t
        expected_sql["reviewed_by"] = person_p
        expected_sql["resolution_note"] = "chat: dismissed"
        target_after = _conflict(target_id)
        assert target_after["reviewed_by"] == person_p
        assert target_after == expected_sql
        assert after_dismiss_business["raw_objects"] == before_dismiss_business["raw_objects"]
        assert after_dismiss_business["knowledge_objects"] == before_dismiss_business["knowledge_objects"]
        assert (
            after_dismiss_business["knowledge_object_versions"]
            == (before_dismiss_business["knowledge_object_versions"])
        )
        others_before = [
            row for row in before_dismiss_business["knowledge_conflicts"] if row["id"] != target_id
        ]
        others_after = [
            row for row in after_dismiss_business["knowledge_conflicts"] if row["id"] != target_id
        ]
        assert others_after == others_before
        assert _ko(target_a)["lifecycle_stage"] == "active"
        assert _ko(target_b)["lifecycle_stage"] == "active"
        assert int(_ko(target_a)["version"]) == 1
        assert int(_ko(target_b)["version"]) == 1
        assert not _ko(target_a)["superseded_by_id"]
        assert not _ko(target_b)["superseded_by_id"]

        assert after_dismiss_audit[: len(before_dismiss_audit)] == before_dismiss_audit
        dismiss_delta = after_dismiss_audit[len(before_dismiss_audit) :]
        assert len(dismiss_delta) == 1
        audit_row = dismiss_delta[0]
        assert audit_id_re.fullmatch(str(audit_row["id"]))
        assert audit_row["user_id"] == person_p
        assert audit_row["action"] == "knowledge_conflict.dismissed"
        assert audit_row["target_type"] == "knowledge_conflict"
        assert audit_row["target_id"] == target_id
        assert audit_row["before_json"] is None
        assert audit_row["ip_address"] == ""
        assert audit_row["request_id"] == decided.headers["x-request-id"]
        created = datetime.fromisoformat(str(audit_row["created_at"]).replace("Z", "+00:00"))
        assert created.tzinfo is not None and created.utcoffset() is not None
        assert t0 - timedelta(seconds=1) <= created <= t1 + timedelta(seconds=1)
        assert _decode(audit_row["after_json"]) == {
            "id": target_id,
            "confidence": 0.9,
            "status": "dismissed",
            "created_at": created_at,
            "reviewed_at": pinned_t,
            "private_fields_count": 5,
            "private_chars": 52,
            "private_items_count": 2,
        }
        _scan(audit_row, target_a, target_b)

        dismissed_item = dict(dismissed_card)
        dismissed_item["triage"] = dict(triage)
        dismissed_list, _, _, _ = _observe(
            lambda: client.get("/api/kg/conflicts?status=dismissed", headers=headers_p),
            audit_delta=0,
        )
        assert dismissed_list.status_code == 200, dismissed_list.text
        assert dismissed_list.json() == _envelope([dismissed_item], status="dismissed", total=1)
        _scan(dismissed_list)

        suggested_again, _, _, _ = _observe(
            lambda: client.get("/api/kg/conflicts?status=suggested", headers=headers_p),
            audit_delta=0,
        )
        assert suggested_again.status_code == 200, suggested_again.text
        assert suggested_again.json() == _envelope([], status="suggested", total=0)
        _scan(suggested_again)

        replay, _, after_replay_audit, replay_delta = _observe(
            lambda: client.post(path, json={"decision": "dismiss"}, headers=headers_p),
            audit_delta=0,
        )
        assert replay.status_code == 409, replay.text
        assert replay.json() == {"detail": "Конфликт уже в статусе dismissed"}
        assert after_replay_audit == after_dismiss_audit
        assert replay_delta == []
        assert _conflict(target_id) == expected_sql
        _scan(replay)

        invalid, _, _, _ = _observe(
            lambda: client.post(path, json={"decision": "not-a-decision"}, headers=headers_p),
            audit_delta=0,
        )
        assert invalid.status_code == 400, invalid.text
        assert invalid.json() == {
            "detail": "decision должен быть dismiss, keep_a или keep_b",
        }
        _scan(invalid)

        missing, _, _, _ = _observe(
            lambda: client.post(
                f"/api/kg/conflicts/{missing_id}/decide",
                json={"decision": "dismiss"},
                headers=headers_p,
            ),
            audit_delta=0,
        )
        assert missing.status_code == 404, missing.text
        assert missing.json() == {"detail": "Конфликт не найден"}
        _scan(missing)

        foreign, _, _, _ = _observe(
            lambda: client.post(
                f"/api/kg/conflicts/{foreign_id}/decide",
                json={"decision": "dismiss"},
                headers=headers_p,
            ),
            audit_delta=0,
        )
        assert foreign.status_code == 404, foreign.text
        assert foreign.json() == {"detail": "Конфликт не найден"}
        _scan(foreign)

        denied, _, _, _ = _observe(
            lambda: client.post(path, json={"decision": "dismiss"}, headers=headers_d),
            audit_delta=0,
        )
        assert denied.status_code == 403, denied.text
        assert denied.json() == {"detail": "Access denied for knowledge.edit (explicit_deny)"}
        _scan(denied)

        before_anon_business = _business()
        before_anon_audit = _audit()
        t_anon0 = datetime.now(UTC)
        anonymous = client.post(path, json={"decision": "dismiss"})
        t_anon1 = datetime.now(UTC)
        after_anon_audit = _audit()
        assert anonymous.status_code == 401, anonymous.text
        assert anonymous.json() == {"detail": "Missing authentication"}
        assert after_anon_audit[: len(before_anon_audit)] == before_anon_audit
        anon_delta = after_anon_audit[len(before_anon_audit) :]
        assert len(anon_delta) == 1
        anon_row = anon_delta[0]
        assert audit_id_re.fullmatch(str(anon_row["id"]))
        assert anon_row["user_id"] == "anonymous"
        assert anon_row["action"] == "auth.failed"
        assert anon_row["target_type"] == "auth"
        assert anon_row["target_id"] == "invalid_credentials"
        assert anon_row["before_json"] is None
        assert anon_row["ip_address"] == ""
        assert anon_row["request_id"] == anonymous.headers["x-request-id"]
        anon_created = datetime.fromisoformat(str(anon_row["created_at"]).replace("Z", "+00:00"))
        assert anon_created.tzinfo is not None and anon_created.utcoffset() is not None
        assert t_anon0 - timedelta(seconds=1) <= anon_created <= t_anon1 + timedelta(seconds=1)
        assert _decode(anon_row["after_json"]) == {
            "method_chars": 4,
            "path_chars": 46,
            "reason": "invalid_credentials",
            "status_present": True,
        }
        assert _business() == before_anon_business
        assert _conflict(target_id) == expected_sql
        _scan(anonymous)
        _scan(anon_row)


@pytest.mark.asyncio
async def test_conflict_tools_list_and_keep_a(settings, storage):
    storage.ensure_user("alice", preset_key="owner")
    conflict_id = _seed_conflict(storage, "alice")
    conflict = storage.get_knowledge_conflict("alice", conflict_id)
    winner = str(conflict["knowledge_a_id"])
    loser = str(conflict["knowledge_b_id"])

    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    web = WebSurfer(settings)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, web, IngestionPipeline(settings, storage, graph))
    actor = auth.actor_for_user("alice", source="test")
    try:
        listed = await kernel.execute("conflict_list", {"limit": 5}, actor=actor)
        assert listed.success is True
        assert listed.data["total"] >= 1
        assert any(item["id"] == conflict_id for item in listed.data["items"])

        # Вердикт по противоречию объявляет одно из знаний устаревшим, поэтому со
        # спеки v3 §5 он идёт через подтверждение человеком: модель предлагает,
        # служба исполняет. Здесь проверяется сам разбор, а не гейт, — цепочка
        # пройдена целиком помощником.
        decided = await run_with_approval(
            kernel,
            storage,
            "conflict_decide",
            {"conflict_id": conflict_id, "decision": "keep_a"},
            actor=actor,
        )
        assert decided.success is True, decided.error
        assert decided.data["status"] == "resolved"
        assert decided.data["winner_id"] == winner

        loser_row = storage.get_knowledge_object(loser, "alice")
        assert str(loser_row.get("lifecycle_stage") or "") == "deprecated"

        listed_after = await kernel.execute("conflict_list", {"limit": 50}, actor=actor)
        assert all(item["id"] != conflict_id for item in listed_after.data["items"])
    finally:
        await web.close()
