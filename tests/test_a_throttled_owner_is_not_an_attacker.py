"""Сигнал безопасности, который горит всегда, не читают.

Замерено на живой базе владельца: из 1302 записей `auth.failed` **1188** оказались
`rate_limited` от самого владельца — он пачкой разбирал Inbox с ВЕРНЫМ токеном с
127.0.0.1 и упирался в ограничитель частоты. Ещё 89 — обращения к `/health`, пути,
которого не существовало: проверка подлинности идёт раньше маршрутизации, поэтому
собственный smoke-check рестарта из runbook проекта возвращал 401 и писался как
отказ аутентификации.

Диагностика при пороге 60 за сутки кричала «возможен брутфорс» постоянно. А под этой
лавиной лежали ТРИ настоящих события: чужая машина 203.0.113.20 стучалась в
`/api/admin/users` и `/api/admin/knowledge`. Их не было видно.

Работа при этом не терялась: все 1184 отбитых элемента Inbox в итоге классифицированы.
Троттлинг делал своё дело — он только притворялся атакой.
"""

from __future__ import annotations

import dataclasses

from fastapi.testclient import TestClient


def _audit(app, action: str) -> list[dict]:
    rows = app.state.storage.execute(
        "SELECT user_id, action, target_id, after_json FROM audit_log "
        "WHERE action=? ORDER BY created_at DESC",
        (action,),
    ).fetchall()
    return [dict(row) for row in rows]


def test_throttling_a_signed_in_caller_is_not_an_authentication_failure(settings):
    """Владелец с верным токеном, упёршийся в частоту, — не взломщик.

    Ограничитель здесь срабатывает ПОСЛЕ успешной аутентификации: у запроса уже есть
    действительный актор. Записывать это как `auth.failed` значит смешивать «кого-то
    не пустили» с «своего придержали».
    """
    from jericho.server import create_app

    tuned = dataclasses.replace(settings, api_user_rate_limit_per_minute=2)
    app = create_app(tuned)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {tuned.api_token}"}
        statuses = [client.get("/api/me", headers=headers).status_code for _ in range(6)]

        assert 429 in statuses, "ограничитель не сработал — проба ничего не проверяет"
        assert not _audit(app, "auth.failed"), "придержанный по частоте СВОЙ записан как отказ аутентификации"
        throttled = _audit(app, "request.throttled")
        assert throttled, "троттлинг не записан вовсе — забыть о нём тоже нельзя"
        assert throttled[0]["target_id"] == "rate_limited"
        assert throttled[0]["user_id"] != "anonymous", (
            "придержали известного пользователя, а записали безымянно — "
            "на нескольких пользователях такая запись бесполезна"
        )


def test_a_wrong_token_is_still_an_authentication_failure(settings):
    """Разделение не должно ослабить настоящий сигнал."""
    from jericho.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        assert (
            client.get("/api/me", headers={"Authorization": "Bearer no-such-token-4f3a2b1c"}).status_code
            == 401
        )

        failures = _audit(app, "auth.failed")
        assert failures and failures[0]["target_id"] == "invalid_credentials"
        assert failures[0]["user_id"] == "anonymous"
        assert "no-such-token-4f3a2b1c" not in (failures[0]["after_json"] or "")


def test_the_health_check_from_the_runbook_answers_without_a_token(settings):
    """`/health` не существовал, и обращение к нему выглядело попыткой взлома.

    Ничего нового наружу не открывается: `/api/health` публичен ровно так же.
    """
    from jericho.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] in {"ok", "starting"}
        assert not _audit(app, "auth.failed"), "проверка здоровья попала в журнал как отказ"


def test_the_two_events_are_told_apart_by_the_burst_check(settings):
    """Диагностика считает ИМЕННО отказы аутентификации.

    Иначе порог выбирается собственной массовой работой владельца, и настоящее
    обращение с чужого адреса тонет в ней бесследно.
    """
    from jericho.diagnostics import collect_diagnostics
    from jericho.server import create_app

    tuned = dataclasses.replace(settings, api_user_rate_limit_per_minute=2)
    app = create_app(tuned)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {tuned.api_token}"}
        for _ in range(8):
            client.get("/api/me", headers=headers)

        result = collect_diagnostics(tuned, storage=app.state.storage)

    keys = {str(item.get("code")) for item in result.get("actions", [])}
    assert "inspect_auth_failure_burst" not in keys, "собственная массовая работа подняла тревогу о брутфорсе"
