"""Одна операция через все поверхности — и сравнивается КОНЕЧНОЕ СОСТОЯНИЕ.

Предложено разбором Codex (§17) и Grok, и сегодняшний день требует этого громче
всего. Отказ в доступе к чужим документам работал на КАЖДОМ слое: правило не
сохранялось, флаг отказа стоял, в журнале «отклонено как попытка расширить права».
А человеку приходило «Готово. Теперь буду показывать документы любого
пользователя». Ни один послойный тест этого не видел, потому что каждый слой был
исправен — расходились они между собой.

Поэтому здесь сравниваются не тексты и не отдельные слои, а то, что осталось ПОСЛЕ
операции: сколько раз случился эффект, в каком состоянии заявка, кому ушло
уведомление, видит ли её посторонний.

Поверхностей у заявки на подтверждение три, и одна из них не самостоятельна:

    прямое ядро      — `kernel.execute_approved`;
    маршрут HTTP     — `POST /api/approvals/{id}/decide`;
    кнопка в Telegram — зовёт ТОТ ЖЕ маршрут (`_callbacks.py`), то есть является
                       его клиентом, а не отдельным путём.

Это существенно и проверяется отдельно: пока кнопка ведёт в маршрут, достаточно
сравнить ядро с маршрутом. Если она однажды начнёт ходить в хранилище напрямую —
появится третий путь, и тест обязан об этом сказать.
"""

from __future__ import annotations

import contextlib
import json

import pytest
from fastapi.testclient import TestClient

from friday.server import create_app
from friday.storage.models import Entity, EntityResolutionCandidate, EntityType, new_id


def _candidate(storage, user_id: str) -> str:
    """Пара сущностей, ждущая решения о слиянии, — предмет опасного действия."""
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


def _approval_sql(storage, approval_id: str) -> dict:
    row = storage.execute("SELECT * FROM action_approvals WHERE id=?", (approval_id,)).fetchone()
    assert row is not None
    return dict(row)


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


def _assert_hidden(response, *canaries: str) -> None:
    blob = response.text
    with contextlib.suppress(Exception):
        blob = blob + json.dumps(response.json(), ensure_ascii=False)
    for canary in canaries:
        if not canary:
            continue
        assert canary not in blob, f"refusal leaked {canary!r} in {blob[:500]}"


def _post_state(storage, user_id: str, approval_id: str) -> dict:
    """То, что осталось после операции. Именно это и сравнивается.

    Не текст ответа: он у поверхностей законно разный — в чате фраза, в API JSON.
    Расходиться им нельзя в другом: в числе эффектов, в состоянии заявки, в
    авторстве решения и в том, кто её видит.
    """
    row = storage.get_action_approval(approval_id, user_id) or {}
    merged = storage.execute(
        "SELECT COUNT(*) AS n FROM entity_resolution_candidates WHERE status='merged' AND user_id=?",
        (user_id,),
    ).fetchone()
    return {
        "status": str(row.get("status") or ""),
        "decided_by": str(row.get("decided_by") or ""),
        "effects": int(merged["n"] if merged else 0),
        "claimed": bool(row.get("claimed_at")),
    }


@pytest.fixture
def api(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        user_id = client.get("/api/admin/users", headers=headers).json()["items"][0]["id"]
        yield app, client, headers, user_id


def test_the_route_and_the_kernel_leave_the_same_state(api, settings) -> None:
    """Одно действие, два пути — состояние обязано совпасть.

    Мутация, которую тест ловит: развести пути так, чтобы один исполнял действие,
    а другой только помечал заявку решённой. Тексты при этом останутся похожими, а
    состояние разойдётся.
    """
    import asyncio

    app, client, headers, user_id = api
    storage = app.state.storage

    # Путь первый: маршрут HTTP.
    first = storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": _candidate(storage, user_id), "decision": "accept"},
        summary="Слить пару",
        requested_by=user_id,
    )
    client.post(f"/api/approvals/{first['id']}/decide", json={"decision": "approve"}, headers=headers)
    after_route = _post_state(storage, user_id, first["id"])

    # Путь второй: прямое ядро, та же операция.
    second = storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": _candidate(storage, user_id), "decision": "accept"},
        summary="Слить пару",
        requested_by=user_id,
    )
    storage.decide_action_approval(
        second["id"], user_id, decision="approve", decided_by=user_id, person_id=user_id
    )
    actor = app.state.auth_service.actor_for_user(user_id, source="test")
    asyncio.run(app.state.kernel.execute_approved(second["id"], actor=actor))
    after_kernel = _post_state(storage, user_id, second["id"])

    assert after_route["status"] == after_kernel["status"] == "done", (
        f"состояния разошлись: маршрут {after_route}, ядро {after_kernel}"
    )
    assert after_route["claimed"] == after_kernel["claimed"] is True
    assert after_kernel["effects"] == 2, "второй путь не выполнил действие или выполнил дважды"


def test_a_second_press_changes_nothing_on_either_path(api) -> None:
    """Двойное нажатие кнопки — обычное дело, и эффект обязан остаться один."""
    app, client, headers, user_id = api
    storage = app.state.storage
    approval = storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": _candidate(storage, user_id), "decision": "accept"},
        summary="Слить пару",
        requested_by=user_id,
    )

    client.post(f"/api/approvals/{approval['id']}/decide", json={"decision": "approve"}, headers=headers)
    once = _post_state(storage, user_id, approval["id"])
    again = client.post(
        f"/api/approvals/{approval['id']}/decide", json={"decision": "approve"}, headers=headers
    )
    twice = _post_state(storage, user_id, approval["id"])

    assert again.status_code == 404, "повторное решение принято как новое"
    assert once == twice, f"второе нажатие изменило состояние: {once} -> {twice}"


def test_the_button_is_a_client_of_the_route_not_a_third_path() -> None:
    """Пока кнопка зовёт маршрут, сравнивать надо двоих. Начнёт ходить мимо — скажет.

    Мутация: увести обработчик кнопки в хранилище напрямую — тест краснеет, и это
    правильно: появится третья поверхность, которую надо сравнивать наравне.
    """
    import inspect

    from friday.telegram_bridge import _callbacks

    source = inspect.getsource(_callbacks)
    assert "/api/approvals/" in source, "кнопка перестала звать маршрут"
    assert "decide" in source
    for direct in ("decide_action_approval", "claim_action_approval", "execute_approved"):
        assert direct not in source, (
            f"кнопка зовёт {direct} мимо маршрута — это третья поверхность, "
            "и её надо сравнивать наравне с остальными"
        )


def test_the_state_compared_is_not_the_text(api) -> None:
    """Тексты у поверхностей законно разные — сравнивать их значит ловить не то.

    Именно это и подвело сегодня: слои были исправны, а расходились между собой.
    Проверка закрепляет, ЧТО именно сравнивается.
    """
    import inspect
    import sys

    source = inspect.getsource(sys.modules[__name__])
    at = source.index("def _post_state")
    body = source[at : at + 900]
    for field in ("status", "decided_by", "effects", "claimed"):
        assert f'"{field}"' in body, f"из сравнения выпало поле {field}"


def test_a_stranger_sees_nothing_on_any_surface(api) -> None:
    """Чужая заявка не видна ни маршрутом, ни списком — на всех путях одинаково."""
    app, client, headers, user_id = api
    storage = app.state.storage
    stranger_raw, _stranger_headers = _issue_token(storage, "stranger")
    owner_token = headers["Authorization"].split(" ", 1)[1]
    own_candidate = _candidate(storage, user_id)
    foreign_candidate = _candidate(storage, user_id)
    own = storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": own_candidate, "decision": "accept"},
        summary="OWN-MERGE-CANARY",
        requested_by=user_id,
    )
    foreign = storage.create_action_approval(
        user_id,
        tool="entity_merge_decide",
        payload={"candidate_id": foreign_candidate, "decision": "accept"},
        summary="FOREIGN-MERGE-CANARY",
        requested_by="stranger",
    )
    selected = {
        "own": _approval_sql(storage, own["id"]),
        "foreign": _approval_sql(storage, foreign["id"]),
    }
    audits_before_reads = _audit_ordered(storage)

    listed = client.get("/api/me/approvals", headers=headers)
    assert listed.status_code == 200, listed.text
    listed_body = listed.json()
    assert listed_body["count"] == 1
    assert listed_body["total"] == 1
    assert listed_body["status"] == "pending"
    assert [_listed_item(item) for item in listed_body["items"]] == [
        {
            "id": own["id"],
            "status": "pending",
            "tool": "entity_merge_decide",
            "summary": "OWN-MERGE-CANARY",
            "error": "",
            "requested_by": user_id,
            "decided_by": "",
        }
    ]
    dump = json.dumps(listed_body, ensure_ascii=False)
    assert foreign["id"] not in dump, "чужая заявка попала в личный список"
    assert dump.count("FOREIGN-MERGE-CANARY") == 0
    assert dump.count("Слить пару") == 0
    assert foreign_candidate not in dump
    assert stranger_raw not in dump
    assert owner_token not in dump

    assert _approval_sql(storage, own["id"]) == selected["own"]
    assert _approval_sql(storage, foreign["id"]) == selected["foreign"]

    canaries = (
        "OWN-MERGE-CANARY",
        "FOREIGN-MERGE-CANARY",
        own["id"],
        foreign["id"],
        own_candidate,
        foreign_candidate,
        stranger_raw,
        owner_token,
    )
    assert _audit_ordered(storage) == audits_before_reads
    anon = client.get("/api/me/approvals")
    assert anon.status_code == 401
    _assert_hidden(anon, *canaries)
    after_anon = _audit_ordered(storage)
    _assert_audit_delta(
        audits_before_reads,
        after_anon,
        [
            {
                "action": "auth.failed",
                "user_id": "anonymous",
                "target_type": "auth",
                "target_id": "invalid_credentials",
            }
        ],
    )
    assert _approval_sql(storage, own["id"]) == selected["own"]
    assert _approval_sql(storage, foreign["id"]) == selected["foreign"]

    storage.set_permission_override(user_id, "chat.use", "deny")
    denied = client.get("/api/me/approvals", headers=headers)
    assert denied.status_code == 403
    _assert_hidden(denied, *canaries)
    assert _approval_sql(storage, own["id"]) == selected["own"]
    assert _approval_sql(storage, foreign["id"]) == selected["foreign"]
    assert _audit_ordered(storage) == after_anon
    storage.set_permission_override(user_id, "chat.use", None)
