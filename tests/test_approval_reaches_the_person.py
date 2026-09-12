"""Заявка доходит до человека и решается там же (спека v3 §5).

Гейт, о котором человек не узнаёт, — это не защита, а тихая поломка: действие
блокируется, а причина видна только тому, кто догадается заглянуть в отдельную
команду. Поэтому проверяется вся цепочка: модель предложила → заявка ушла в чат
С КНОПКАМИ → нажатие исполнило действие → повторное нажатие не исполнило его
второй раз.

Отдельно проверяется, что «решение записано» и «действие выполнено» — разные
факты. Подтверждённое действие может не состояться (право отобрали, аргументы
изменились, сбой), и выдавать первое за второе нельзя.
"""

from __future__ import annotations

import contextlib
import json

import pytest
from fastapi.testclient import TestClient

from friday.server import create_app
from friday.storage.models import Entity, EntityResolutionCandidate, EntityType, new_id


def _pending_as_bridge(client, settings) -> dict:
    """Очередь глазами моста: маршрут отдаёт её только по подписи моста."""
    import time
    import uuid

    from friday.security import sign_bridge_request

    path = "/api/notifications/pending?limit=20"
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    signer = "42"
    response = client.get(
        path,
        headers={
            "X-Friday-Timestamp": str(timestamp),
            "X-Friday-User": signer,
            "X-Friday-Chat": signer,
            "X-Friday-Nonce": nonce,
            "X-Friday-Signature": sign_bridge_request(
                settings.telegram_bridge_secret,
                timestamp=timestamp,
                method="GET",
                path=path,
                external_user_id=signer,
                chat_id=signer,
                nonce=nonce,
                body=b"",
            ),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _candidate(storage, user_id: str) -> str:
    left = Entity(id=new_id("ent"), user_id=user_id, name="Иванов И.И.", entity_type=EntityType.PERSON)
    right = Entity(id=new_id("ent"), user_id=user_id, name="Иванов Иван", entity_type=EntityType.PERSON)
    storage.create_entity(left)
    storage.create_entity(right)
    return storage.store_resolution_candidate(
        EntityResolutionCandidate(
            id=new_id("res"),
            user_id=user_id,
            entity_a_id=left.id,
            entity_b_id=right.id,
            confidence=0.9,
            resolution_method="name_similarity",
            evidence_json={"reason": "похожие имена"},
        )
    ).id


def _merge_seed(storage, user_id: str) -> dict:
    left = Entity(id=new_id("ent"), user_id=user_id, name="Иванов И.И.", entity_type=EntityType.PERSON)
    right = Entity(id=new_id("ent"), user_id=user_id, name="Иванов Иван", entity_type=EntityType.PERSON)
    storage.create_entity(left)
    storage.create_entity(right)
    candidate = storage.store_resolution_candidate(
        EntityResolutionCandidate(
            id=new_id("res"),
            user_id=user_id,
            entity_a_id=left.id,
            entity_b_id=right.id,
            confidence=0.9,
            resolution_method="name_similarity",
            evidence_json={"reason": "похожие имена"},
        )
    )
    return {
        "left_id": left.id,
        "right_id": right.id,
        "candidate_id": candidate.id,
    }


def _sql_row(storage, table: str, row_id: str) -> dict:
    allowed = {
        "action_approvals": "action_approvals",
        "entity_resolution_candidates": "entity_resolution_candidates",
        "entities": "entities",
    }
    row = storage.execute(f"SELECT * FROM {allowed[table]} WHERE id=?", (row_id,)).fetchone()
    assert row is not None, f"{table} {row_id} missing"
    return dict(row)


def _audit_ordered(storage) -> list[dict]:
    rows = storage.execute(
        "SELECT id, user_id, action, target_type, target_id, before_json, after_json, "
        "ip_address, request_id, created_at FROM audit_log ORDER BY rowid ASC"
    ).fetchall()
    return [dict(row) for row in rows]


def _audit_known(row: dict) -> dict:
    return {
        "action": row["action"],
        "user_id": row["user_id"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
    }


def _assert_audit_delta(before: list[dict], after: list[dict], expected: list[dict]) -> list[dict]:
    assert after[: len(before)] == before
    delta = after[len(before) :]
    observed = [_audit_known(row) for row in delta]
    assert observed == expected, observed
    return delta


def _json_object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        return json.loads(value)
    return {}


def _selected_state(
    storage,
    *,
    approval_id: str,
    candidate_id: str | None = None,
    left_id: str | None = None,
    right_id: str | None = None,
) -> dict:
    state = {"approval": _sql_row(storage, "action_approvals", approval_id)}
    if candidate_id:
        state["candidate"] = _sql_row(storage, "entity_resolution_candidates", candidate_id)
    if left_id:
        state["left"] = _sql_row(storage, "entities", left_id)
    if right_id:
        state["right"] = _sql_row(storage, "entities", right_id)
    return state


def _issue_token(storage, user_id: str, *, preset_key: str = "user") -> tuple[str, dict[str, str]]:
    import hashlib
    import secrets

    storage.ensure_user(user_id, preset_key=preset_key)
    raw = secrets.token_urlsafe(32)
    storage.create_api_token(
        user_id, hashlib.sha256(raw.encode()).hexdigest(), label="lab-approval", ttl_seconds=3600
    )
    return raw, {"Authorization": f"Bearer {raw}"}


def _listed_item(item: dict) -> dict:
    return {
        "id": item["id"],
        "status": item["status"],
        "tool": item["tool"],
        "summary": item["summary"],
        "error": item.get("error") or "",
        "requested_by": item.get("requested_by") or "",
        "decided_by": item.get("decided_by") or "",
    }


def _assert_hidden(response, *canaries: str) -> None:
    blob = response.text
    with contextlib.suppress(Exception):
        blob = blob + json.dumps(response.json(), ensure_ascii=False)
    for canary in canaries:
        if not canary:
            continue
        assert canary not in blob, f"refusal leaked {canary!r} in {blob[:500]}"


def _merge_effect(storage, user_id: str, seed: dict) -> None:
    candidate = storage.get_resolution_candidate(seed["candidate_id"], user_id)
    assert candidate is not None
    assert str(candidate["status"]) == "merged"
    left = _sql_row(storage, "entities", seed["left_id"])
    right = _sql_row(storage, "entities", seed["right_id"])
    # accept_resolution without target_entity_id keeps the richer, then older
    # entity_a (left). The source (right) must point at that distinct id.
    assert left["id"] == seed["left_id"]
    assert right["id"] == seed["right_id"]
    assert left["id"] != right["id"]
    assert str(left.get("merged_into_id") or "") == ""
    assert str(right.get("merged_into_id") or "") == seed["left_id"]
    assert str(right.get("merged_into_id") or "") != right["id"]
    assert int(left.get("canonical") or 0) == 1
    assert int(right.get("canonical") or 0) == 0
    assert not left.get("deleted_at")
    assert right.get("deleted_at")


@pytest.fixture
def api(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        user_id = client.get("/api/admin/users", headers=headers).json()["items"][0]["id"]
        yield app, client, headers, user_id


@pytest.mark.asyncio
async def test_a_request_is_pushed_to_the_person_with_buttons(settings, storage):
    """Мутация: убрать постановку уведомления в `_request_approval` — тест краснеет."""
    from friday.execution_kernel import ExecutionKernel
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph
    from friday.permissions import AuthorizationService
    from friday.web_surfer import WebSurfer

    storage.ensure_user("alice", preset_key="owner")
    # Чат, в который проактивные сообщения вообще доставляются.
    storage.update_user("alice", metadata_json=json.dumps({"chat_id": "42"}))
    candidate_id = _candidate(storage, "alice")

    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))
    actor = auth.actor_for_user("alice", source="test")

    requested = await kernel.execute(
        "entity_merge_decide", {"candidate_id": candidate_id, "decision": "accept"}, actor=actor
    )
    approval_id = requested.data["approval_id"]

    pending = storage.list_pending_notifications(limit=10)
    mine = [row for row in pending if row.get("kind") == "approval"]
    assert mine, "заявка создана, но человек о ней не узнает"
    assert mine[0]["dedup_key"] == f"approval:{approval_id}", (
        "у уведомления нет ссылки на заявку — кнопку решения по нему не построить"
    )
    assert "решение" in mine[0]["body"].casefold()


def test_the_route_executes_on_approval_and_only_once(api):
    app, client, headers, user_id = api
    storage = app.state.storage
    seed = _merge_seed(storage, user_id)
    summary = "Слить «Иванов И.И.» и «Иванов Иван»"
    approval = storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": seed["candidate_id"], "decision": "accept"},
        summary=summary,
        requested_by=user_id,
    )

    listed = client.get("/api/me/approvals", headers=headers)
    assert listed.status_code == 200, listed.text
    listed_body = listed.json()
    assert listed_body["count"] == 1
    assert listed_body["total"] == 1
    assert listed_body["status"] == "pending"
    assert [_listed_item(item) for item in listed_body["items"]] == [
        {
            "id": approval["id"],
            "status": "pending",
            "tool": "entity_merge_decide",
            "summary": summary,
            "error": "",
            "requested_by": user_id,
            "decided_by": "",
        }
    ]

    audits_before = _audit_ordered(storage)
    before_entities = _selected_state(
        storage,
        approval_id=approval["id"],
        candidate_id=seed["candidate_id"],
        left_id=seed["left_id"],
        right_id=seed["right_id"],
    )
    assert before_entities["approval"]["status"] == "pending"
    assert before_entities["approval"]["requested_by"] == user_id
    assert before_entities["candidate"]["status"] == "suggested"

    response = client.post(
        f"/api/approvals/{approval['id']}/decide", json={"decision": "approve"}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["executed"] is True, body.get("error")
    assert body["approval"]["id"] == approval["id"]
    assert body["approval"]["status"] == "done"
    assert body["approval"]["decided_by"] == user_id
    assert body["approval"]["requested_by"] == user_id
    assert body["approval"]["tool"] == "entity_merge_decide"
    assert body["approval"]["summary"] == summary

    stored = storage.get_action_approval(approval["id"], user_id, person_id=user_id)
    assert stored is not None
    assert stored["id"] == approval["id"]
    assert stored["status"] == "done"
    assert stored["decided_by"] == user_id
    assert stored["requested_by"] == user_id
    assert stored["claimed_at"]
    sql_approval = _sql_row(storage, "action_approvals", approval["id"])
    assert sql_approval["status"] == "done"
    assert sql_approval["decided_by"] == user_id
    _merge_effect(storage, user_id, seed)

    pending = client.get("/api/me/approvals?status=pending", headers=headers)
    assert pending.status_code == 200, pending.text
    pending_body = pending.json()
    assert pending_body["count"] == 0
    assert pending_body["total"] == 0
    assert pending_body["status"] == "pending"
    assert pending_body["items"] == []

    done = client.get("/api/me/approvals?status=done", headers=headers)
    assert done.status_code == 200, done.text
    done_body = done.json()
    assert done_body["count"] == 1
    assert done_body["total"] == 1
    assert done_body["status"] == "done"
    assert [_listed_item(item) for item in done_body["items"]] == [
        {
            "id": approval["id"],
            "status": "done",
            "tool": "entity_merge_decide",
            "summary": summary,
            "error": "",
            "requested_by": user_id,
            "decided_by": user_id,
        }
    ]

    audits_after = _audit_ordered(storage)
    delta = _assert_audit_delta(
        audits_before,
        audits_after,
        [
            {
                "action": "approval.approve",
                "user_id": user_id,
                "target_type": "action_approval",
                "target_id": approval["id"],
            },
            {
                "action": "tool.invoke",
                "user_id": user_id,
                "target_type": "tool",
                "target_id": "entity_merge_decide",
            },
        ],
    )
    approve_after = _json_object(delta[0]["after_json"])
    assert approve_after["id"] == approval["id"]
    assert approve_after["user_id"] == user_id
    tool_after = _json_object(delta[1]["after_json"])
    assert tool_after["success"] is True
    assert tool_after["reason"] == "ok_approved"
    assert tool_after["approval"] == approval["id"]
    after_success = _selected_state(
        storage,
        approval_id=approval["id"],
        candidate_id=seed["candidate_id"],
        left_id=seed["left_id"],
        right_id=seed["right_id"],
    )

    # Повторное нажатие той же кнопки — обычное дело в чате, и оно не должно ни
    # выполнять действие второй раз, ни выглядеть поломкой.
    again = client.post(
        f"/api/approvals/{approval['id']}/decide", json={"decision": "approve"}, headers=headers
    )
    assert again.status_code == 404
    assert again.json() == {"detail": "Заявка не найдена или уже решена"}
    after_replay = _selected_state(
        storage,
        approval_id=approval["id"],
        candidate_id=seed["candidate_id"],
        left_id=seed["left_id"],
        right_id=seed["right_id"],
    )
    assert after_replay == after_success
    assert _audit_ordered(storage) == audits_after


def test_a_rejection_does_not_execute(api):
    app, client, headers, user_id = api
    storage = app.state.storage
    seed = _merge_seed(storage, user_id)
    summary = "Слить два узла"
    approval = storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": seed["candidate_id"], "decision": "accept"},
        summary=summary,
        requested_by=user_id,
    )
    before = _selected_state(
        storage,
        approval_id=approval["id"],
        candidate_id=seed["candidate_id"],
        left_id=seed["left_id"],
        right_id=seed["right_id"],
    )
    audits_before = _audit_ordered(storage)

    response = client.post(
        f"/api/approvals/{approval['id']}/decide", json={"decision": "reject"}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["executed"] is False
    assert body["approval"]["id"] == approval["id"]
    assert body["approval"]["status"] == "rejected"
    assert body["approval"]["decided_by"] == user_id
    assert body["approval"]["requested_by"] == user_id
    stored = storage.get_action_approval(approval["id"], user_id, person_id=user_id)
    assert stored["id"] == approval["id"]
    assert stored["status"] == "rejected"
    assert stored["decided_by"] == user_id
    assert not stored.get("claimed_at")
    sql_approval = _sql_row(storage, "action_approvals", approval["id"])
    assert sql_approval["status"] == "rejected"
    assert sql_approval["decided_by"] == user_id
    assert not sql_approval.get("claimed_at")

    rejected = client.get("/api/me/approvals?status=rejected", headers=headers)
    assert rejected.status_code == 200, rejected.text
    rejected_body = rejected.json()
    assert rejected_body["count"] == 1
    assert rejected_body["total"] == 1
    assert rejected_body["status"] == "rejected"
    assert [_listed_item(item) for item in rejected_body["items"]] == [
        {
            "id": approval["id"],
            "status": "rejected",
            "tool": "entity_merge_decide",
            "summary": summary,
            "error": "",
            "requested_by": user_id,
            "decided_by": user_id,
        }
    ]
    pending = client.get("/api/me/approvals?status=pending", headers=headers)
    assert pending.status_code == 200, pending.text
    pending_body = pending.json()
    assert pending_body["count"] == 0
    assert pending_body["total"] == 0
    assert pending_body["items"] == []

    after = _selected_state(
        storage,
        approval_id=approval["id"],
        candidate_id=seed["candidate_id"],
        left_id=seed["left_id"],
        right_id=seed["right_id"],
    )
    assert after["candidate"] == before["candidate"]
    assert after["left"] == before["left"]
    assert after["right"] == before["right"]
    assert str(storage.get_resolution_candidate(seed["candidate_id"], user_id)["status"]) == "suggested"
    audits_after = _audit_ordered(storage)
    delta = _assert_audit_delta(
        audits_before,
        audits_after,
        [
            {
                "action": "approval.reject",
                "user_id": user_id,
                "target_type": "action_approval",
                "target_id": approval["id"],
            }
        ],
    )
    reject_after = _json_object(delta[0]["after_json"])
    assert reject_after["id"] == approval["id"]
    assert reject_after["user_id"] == user_id
    assert reject_after["status"] == "rejected"


def test_an_approval_that_cannot_execute_says_so(api):
    """Согласие человека и успех исполнения — разные факты.

    Мутация: возвращать `executed: True` без проверки `result.success` — тест
    краснеет.
    """
    app, client, headers, user_id = api
    storage = app.state.storage
    missing_candidate = "res_does_not_exist"
    approval = storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": missing_candidate, "decision": "accept"},
        summary="Слить несуществующее",
        requested_by=user_id,
    )
    audits_before = _audit_ordered(storage)
    response = client.post(
        f"/api/approvals/{approval['id']}/decide", json={"decision": "approve"}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["executed"] is False
    http_error = body.get("error") or ""
    assert http_error == "Tool failed: ValueError", http_error
    assert body["approval"]["id"] == approval["id"]
    assert body["approval"]["status"] == "failed"
    assert body["approval"]["decided_by"] == user_id
    assert body["approval"]["requested_by"] == user_id
    stored = storage.get_action_approval(approval["id"], user_id, person_id=user_id)
    assert stored["id"] == approval["id"]
    assert stored["status"] == "failed"
    assert stored["decided_by"] == user_id
    assert stored["requested_by"] == user_id
    stored_error = stored["error"] or ""
    assert stored_error == "ValueError", stored_error
    assert (body["approval"].get("error") or "") == stored_error
    sql_approval = _sql_row(storage, "action_approvals", approval["id"])
    assert sql_approval["status"] == "failed"
    assert sql_approval["decided_by"] == user_id
    assert sql_approval["error"] == stored_error

    failed = client.get("/api/me/approvals?status=failed", headers=headers)
    assert failed.status_code == 200, failed.text
    failed_body = failed.json()
    assert failed_body["count"] == 1
    assert failed_body["total"] == 1
    assert failed_body["status"] == "failed"
    assert [_listed_item(item) for item in failed_body["items"]] == [
        {
            "id": approval["id"],
            "status": "failed",
            "tool": "entity_merge_decide",
            "summary": "Слить несуществующее",
            "error": stored_error,
            "requested_by": user_id,
            "decided_by": user_id,
        }
    ]
    pending = client.get("/api/me/approvals?status=pending", headers=headers)
    assert pending.status_code == 200, pending.text
    assert pending.json()["count"] == 0
    assert pending.json()["total"] == 0
    assert storage.get_resolution_candidate(missing_candidate, user_id) is None
    audits_after = _audit_ordered(storage)
    delta = _assert_audit_delta(
        audits_before,
        audits_after,
        [
            {
                "action": "approval.approve",
                "user_id": user_id,
                "target_type": "action_approval",
                "target_id": approval["id"],
            },
            {
                "action": "tool.invoke",
                "user_id": user_id,
                "target_type": "tool",
                "target_id": "entity_merge_decide",
            },
        ],
    )
    fail_approve_after = _json_object(delta[0]["after_json"])
    assert fail_approve_after["id"] == approval["id"]
    assert fail_approve_after["user_id"] == user_id
    fail_tool_after = _json_object(delta[1]["after_json"])
    assert fail_tool_after["success"] is False
    assert fail_tool_after.get("reason_chars") == 10
    assert "reason" not in fail_tool_after


def test_another_tenant_cannot_decide_your_action(api):
    app, client, headers, user_id = api
    storage = app.state.storage
    approval = storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": "res_1", "decision": "accept"},
        summary="Слить два узла",
    )
    storage.ensure_user("mallory", preset_key="user")
    import hashlib
    import secrets

    raw_token = secrets.token_urlsafe(32)
    storage.create_api_token(
        "mallory", hashlib.sha256(raw_token.encode()).hexdigest(), label="test", ttl_seconds=3600
    )
    other = {"Authorization": f"Bearer {raw_token}"}

    response = client.post(
        f"/api/approvals/{approval['id']}/decide", json={"decision": "approve"}, headers=other
    )
    assert response.status_code in {403, 404}
    assert storage.get_action_approval(approval["id"], user_id)["status"] == "pending"


def test_the_chat_command_lists_what_waits_and_names_unknown_outcomes(api):
    """`/approvals` — единственное место, где видно и ожидающее, и неизвестное."""
    app, client, headers, user_id = api
    storage = app.state.storage
    waiting_row = storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": "res_1", "decision": "accept"},
        summary="Слить два узла",
        requested_by=user_id,
    )
    stale = storage.create_action_approval(
        user_id,
        tool="code_run",
        payload={"code": "print(1)"},
        summary="Выполнить код",
        requested_by=user_id,
    )
    storage.decide_action_approval(
        stale["id"], user_id, decision="approve", decided_by=user_id, person_id=user_id
    )
    storage.claim_action_approval(stale["id"], user_id)
    storage.mark_action_approval_uncertain(stale["id"], user_id, error="прервано")
    audits_before_gets = _audit_ordered(storage)

    waiting = client.get("/api/me/approvals?status=pending", headers=headers)
    assert waiting.status_code == 200, waiting.text
    waiting_body = waiting.json()
    assert waiting_body["count"] == 1
    assert waiting_body["total"] == 1
    assert waiting_body["status"] == "pending"
    assert [_listed_item(item) for item in waiting_body["items"]] == [
        {
            "id": waiting_row["id"],
            "status": "pending",
            "tool": "entity_merge_decide",
            "summary": "Слить два узла",
            "error": "",
            "requested_by": user_id,
            "decided_by": "",
        }
    ]

    unknown = client.get("/api/me/approvals?status=uncertain", headers=headers)
    assert unknown.status_code == 200, unknown.text
    unknown_body = unknown.json()
    assert unknown_body["count"] == 1, "действие с неизвестным исходом нигде не видно"
    assert unknown_body["total"] == 1, "действие с неизвестным исходом нигде не видно"
    assert unknown_body["status"] == "uncertain"
    assert [_listed_item(item) for item in unknown_body["items"]] == [
        {
            "id": stale["id"],
            "status": "uncertain",
            "tool": "code_run",
            "summary": "Выполнить код",
            "error": "прервано",
            "requested_by": user_id,
            "decided_by": user_id,
        }
    ]

    newer = storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": "res_2", "decision": "accept"},
        summary="Слить вторую пару",
        requested_by=user_id,
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE action_approvals SET created_at=? WHERE id=?",
            ("2020-01-01T00:00:00", waiting_row["id"]),
        )
        conn.execute(
            "UPDATE action_approvals SET created_at=? WHERE id=?",
            ("2020-06-01T00:00:00", newer["id"]),
        )

    page = client.get("/api/me/approvals?status=pending&limit=1", headers=headers)
    assert page.status_code == 200, page.text
    page_body = page.json()
    assert page_body["count"] == 1
    assert page_body["total"] == 2
    assert page_body["status"] == "pending"
    assert [_listed_item(item) for item in page_body["items"]] == [
        {
            "id": newer["id"],
            "status": "pending",
            "tool": "entity_merge_decide",
            "summary": "Слить вторую пару",
            "error": "",
            "requested_by": user_id,
            "decided_by": "",
        }
    ]
    assert _audit_ordered(storage) == audits_before_gets


def test_the_bridge_receives_what_it_needs_to_draw_the_buttons(api):
    """Проверяется ВЫДАЧА мосту, а не строка в таблице.

    Между «в очереди лежит правильная запись» и «мост может нарисовать кнопку»
    стоит маршрут `/api/notifications/pending`, который сам решает, какие поля
    отдать. Пока он отдавал только id/chat_id/body, заявка приходила человеку
    текстом «нужно ваше решение», а решить её в этом же сообщении было нечем.
    """
    app, client, headers, user_id = api
    storage = app.state.storage
    approval = storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": "res_1", "decision": "accept"},
        summary="Слить два узла",
    )
    storage.update_user(user_id, metadata_json=json.dumps({"chat_id": "42"}))
    storage.enqueue_notification(
        user_id,
        "42",
        f"Нужно ваше решение: {approval['summary']}",
        kind="approval",
        dedup_key=f"approval:{approval['id']}",
    )

    items = _pending_as_bridge(client, app.state.settings)["items"]
    mine = [item for item in items if item.get("kind") == "approval"]
    assert mine, "мост не узнает, что это заявка, и отправит её без кнопок"
    assert mine[0]["dedup_key"] == f"approval:{approval['id']}", (
        "мосту нечего подставить в callback_data — кнопка решения не построится"
    )


def test_a_bystander_pressing_the_button_changes_nothing(api):
    """Кнопка в общей комнате доступна всем, кто её видит.

    Callback приходит от того, КТО НАЖАЛ, а маршрут решает от имени этого актора.
    Значит защита — не в кнопке, а в том, что заявка принадлежит владельцу: чужому
    маршрут обязан ответить «не найдено», не подтвердив даже её существования.

    Мутация: искать заявку без `user_id` — тест краснеет.
    """
    app, client, headers, user_id = api
    storage = app.state.storage
    seed = _merge_seed(storage, user_id)
    summary = "Слить два узла"
    approval = storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": seed["candidate_id"], "decision": "accept"},
        summary=summary,
        requested_by=user_id,
    )
    owner_token = headers["Authorization"].split(" ", 1)[1]
    bystander_raw, bystander_headers = _issue_token(storage, "bystander")
    canaries = (
        summary,
        seed["candidate_id"],
        seed["left_id"],
        seed["right_id"],
        bystander_raw,
        owner_token,
    )
    missing_id = "apr_does_not_exist"

    audits_before_refusals = _audit_ordered(storage)
    listed = client.get("/api/me/approvals", headers=bystander_headers)
    assert listed.status_code == 200, listed.text
    listed_body = listed.json()
    assert listed_body["count"] == 0
    assert listed_body["total"] == 0
    assert listed_body["items"] == []
    _assert_hidden(listed, summary, seed["candidate_id"], bystander_raw, owner_token)
    assert _audit_ordered(storage) == audits_before_refusals

    snapshot = _selected_state(
        storage,
        approval_id=approval["id"],
        candidate_id=seed["candidate_id"],
        left_id=seed["left_id"],
        right_id=seed["right_id"],
    )

    foreign = client.post(
        f"/api/approvals/{approval['id']}/decide",
        json={"decision": "approve"},
        headers=bystander_headers,
    )
    missing = client.post(
        f"/api/approvals/{missing_id}/decide",
        json={"decision": "approve"},
        headers=bystander_headers,
    )
    assert foreign.status_code == 404
    assert missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": "Заявка не найдена или уже решена"}
    _assert_hidden(foreign, *canaries)
    _assert_hidden(missing, *canaries)
    assert (
        _selected_state(
            storage,
            approval_id=approval["id"],
            candidate_id=seed["candidate_id"],
            left_id=seed["left_id"],
            right_id=seed["right_id"],
        )
        == snapshot
    )
    assert _audit_ordered(storage) == audits_before_refusals

    anon_get = client.get("/api/me/approvals")
    after_anon_get = _audit_ordered(storage)
    _assert_audit_delta(
        audits_before_refusals,
        after_anon_get,
        [
            {
                "action": "auth.failed",
                "user_id": "anonymous",
                "target_type": "auth",
                "target_id": "invalid_credentials",
            }
        ],
    )
    anon_post = client.post(f"/api/approvals/{approval['id']}/decide", json={"decision": "approve"})
    after_anon = _audit_ordered(storage)
    _assert_audit_delta(
        audits_before_refusals,
        after_anon,
        [
            {
                "action": "auth.failed",
                "user_id": "anonymous",
                "target_type": "auth",
                "target_id": "invalid_credentials",
            },
            {
                "action": "auth.failed",
                "user_id": "anonymous",
                "target_type": "auth",
                "target_id": "invalid_credentials",
            },
        ],
    )
    assert anon_get.status_code == 401
    assert anon_post.status_code == 401
    _assert_hidden(anon_get, *canaries)
    _assert_hidden(anon_post, *canaries)
    assert (
        _selected_state(
            storage,
            approval_id=approval["id"],
            candidate_id=seed["candidate_id"],
            left_id=seed["left_id"],
            right_id=seed["right_id"],
        )
        == snapshot
    )

    invalid = client.post(
        f"/api/approvals/{approval['id']}/decide",
        json={"decision": "maybe"},
        headers=headers,
    )
    assert invalid.status_code == 400
    assert invalid.json() == {"detail": "Решение должно быть approve или reject"}
    _assert_hidden(invalid, *canaries)
    assert (
        _selected_state(
            storage,
            approval_id=approval["id"],
            candidate_id=seed["candidate_id"],
            left_id=seed["left_id"],
            right_id=seed["right_id"],
        )
        == snapshot
    )
    assert _audit_ordered(storage) == after_anon

    storage.set_permission_override(user_id, "chat.use", "deny")
    denied_get = client.get("/api/me/approvals", headers=headers)
    denied_post = client.post(
        f"/api/approvals/{approval['id']}/decide",
        json={"decision": "approve"},
        headers=headers,
    )
    assert denied_get.status_code == 403
    assert denied_post.status_code == 403
    _assert_hidden(denied_get, *canaries)
    _assert_hidden(denied_post, *canaries)
    assert (
        _selected_state(
            storage,
            approval_id=approval["id"],
            candidate_id=seed["candidate_id"],
            left_id=seed["left_id"],
            right_id=seed["right_id"],
        )
        == snapshot
    )
    assert _audit_ordered(storage) == after_anon
    storage.set_permission_override(user_id, "chat.use", None)
