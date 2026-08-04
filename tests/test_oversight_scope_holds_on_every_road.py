"""Право надзора не означает право на ЛЮБОГО — и это верно на всех дорогах.

Проверка «вижу себя и своих подчинённых» стояла на двух дорогах агента
(`user_activity`, `user_knowledge_search`) и не стояла на HTTP-дороге
`/api/admin/users/{id}/activity`, которая показывает ровно ту же деятельность
того же человека, только через админку. Ворота на одной дороге не охраняют
ничего.

Сегодня эта проверка никого не задевает. Владелец 2026-08-04, отвечая на прямой
вопрос о живой установке (руководителей ноль, значит каждый видит любого),
решил: **«все видят всех»**. Пока никому не назначен руководитель,
`hierarchy_is_configured` ложно, и правило не применяется вовсе — иначе введение
поля молча выключило бы работающий надзор.

Смысл проверки в другом: в день, когда руководителя назначат, дверь через
админку не должна остаться распахнутой молча. Поэтому здесь проверяются ОБА
состояния — и решение владельца, и включённая иерархия.

Решение «все видят всех» — про СМОТРЕТЬ. Право менять чужую учётку и управлять
чужой работой оно не открывает: там граница по хозяину архива, см.
`test_a_full_preset_is_not_the_archive_owner`.
"""

from __future__ import annotations

import json

import pytest

from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext, AuthorizationService

VIEWER = "telegram:telegram:5344917795"
STRANGER = "telegram:telegram:8696167804"


def _request(storage, auth, actor: ActorContext):
    class _Request:
        def __init__(self) -> None:
            self.app = type(
                "App", (), {"state": type("S", (), {"storage": storage, "auth_service": auth})()}
            )()
            self.state = type("RS", (), {"actor": actor, "client_ip": "", "request_id": ""})()

    return _Request()


@pytest.fixture
def people(storage):
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    storage.ensure_user(VIEWER, preset_key="owner")
    storage.ensure_user(STRANGER, preset_key="owner")
    storage.commit()
    return storage


def _viewer() -> ActorContext:
    return ActorContext(
        user_id=LEGACY_OWNER_USER_ID,
        preset_key="owner",
        source="api-token",
        shared_tenant=True,
        person_id=VIEWER,
    )


@pytest.mark.asyncio
async def test_everyone_sees_everyone_until_a_supervisor_is_named(people):
    """Решение владельца. Мутация: убрать `hierarchy_is_configured` — краснеет."""
    from friday.admin_api._users import user_activity

    auth = AuthorizationService(people, shared_tenant=LEGACY_OWNER_USER_ID)
    answer = await user_activity(
        STRANGER, _request(people, auth, _viewer()), since=None, until=None, limit=10, offset=0, analysis=[], top=10
    )

    assert answer["user_id"] == STRANGER


@pytest.mark.asyncio
async def test_a_named_hierarchy_closes_the_admin_road_too(people):
    """Как только руководитель назначен, правило работает и здесь.

    Мутация: снять проверку с HTTP-дороги — тест краснеет, и чужая деятельность
    снова читается через админку в обход ворот, стоящих у агента.
    """
    from fastapi import HTTPException

    from friday.admin_api._users import user_activity

    # Разметка появилась: у постороннего есть руководитель, и это не наблюдатель.
    people.update_user(STRANGER, metadata_json={"supervisor_id": LEGACY_OWNER_USER_ID})
    people.commit()
    auth = AuthorizationService(people, shared_tenant=LEGACY_OWNER_USER_ID)

    with pytest.raises(HTTPException) as denied:
        await user_activity(
            STRANGER,
            _request(people, auth, _viewer()),
            since=None,
            until=None,
            limit=10,
            offset=0,
            analysis=[],
            top=10,
        )

    assert denied.value.status_code == 403
    assert "подчинённый" in str(denied.value.detail)


@pytest.mark.asyncio
async def test_a_supervisor_still_sees_his_own_people(people):
    """Ошибка в другую сторону: правило не должно отнимать положенное."""
    from friday.admin_api._users import user_activity

    people.update_user(STRANGER, metadata_json={"supervisor_id": VIEWER})
    people.commit()
    auth = AuthorizationService(people, shared_tenant=LEGACY_OWNER_USER_ID)

    answer = await user_activity(
        STRANGER, _request(people, auth, _viewer()), since=None, until=None, limit=10, offset=0, analysis=[], top=10
    )

    assert answer["user_id"] == STRANGER


@pytest.mark.asyncio
async def test_the_refusal_leaves_a_trail(people):
    """Отказ в надзоре записывается: иначе попытку не отличить от бездействия."""
    from fastapi import HTTPException

    from friday.admin_api._users import user_activity

    people.update_user(STRANGER, metadata_json={"supervisor_id": LEGACY_OWNER_USER_ID})
    people.commit()
    auth = AuthorizationService(people, shared_tenant=LEGACY_OWNER_USER_ID)

    with pytest.raises(HTTPException):
        await user_activity(
            STRANGER,
            _request(people, auth, _viewer()),
            since=None,
            until=None,
            limit=10,
            offset=0,
            analysis=[],
            top=10,
        )

    trail = " ".join(json.dumps(row, ensure_ascii=False, default=str) for row in people.list_audit_log(limit=10))
    assert "out_of_scope" in trail, f"попытка посмотреть чужое не записана: {trail!r}"
