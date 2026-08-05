"""«Полные права» и «хозяин архива» — разные вещи.

Владелец просил заводить каждого написавшего с ПОЛНЫМИ правами, и на живой
установке пресет `owner` стоит у шести учёток: две принадлежат владельцу (через
API и через бота), четыре — участникам. А `ActorContext.is_owner` смотрел только
на пресет — то есть хозяином архива система считала каждого из шестерых.

Воспроизведено на изолированном стенде до правки; все четыре замка пропустили
участника:

    require_delegable_account → выпустить API-токен на учётку владельца,
    _protect_owner_target     → изменить учётку владельца,
    set_user_preset("owner")  → раздать пресет владельца кому угодно,
    _only_mine                → управлять чужими миссиями.

Первый из них — прямая эскалация: токен это ВСЯ власть учётки, и участник
получал её на учётку хозяина, после чего разница между ними исчезала совсем.

Владелец 2026-08-04 решил: «все видят всех». Решение про СМОТРЕТЬ, и надзор здесь
не трогается — трогается право МЕНЯТЬ чужую учётку, которое досталось всем
заодно, потому что одно слово `owner` называло два разных понятия.

Граница проведена по учётке, а не по пресету: в общем архиве хозяин тот, чей
`own_id` совпадает с арендатором. В личном архиве (`shared_tenant=False`) ничего
не меняется — там пресет `owner` и есть хозяин, и это единственный человек.
"""

from __future__ import annotations

import pytest

from friday.permissions import (
    LEGACY_OWNER_USER_ID,
    ActorContext,
    AuthorizationError,
    AuthorizationService,
)

#: Участник живой установки: полные права, общий архив, СВОЙ человек.
PARTICIPANT = "telegram:telegram:5344917795"


def _participant() -> ActorContext:
    return ActorContext(
        user_id=LEGACY_OWNER_USER_ID,
        preset_key="owner",
        source="telegram-bridge",
        shared_tenant=True,
        person_id=PARTICIPANT,
    )


def _archive_owner() -> ActorContext:
    """Владелец: тот, чей человек и есть арендатор."""
    return ActorContext(
        user_id=LEGACY_OWNER_USER_ID,
        preset_key="owner",
        source="api-token",
        shared_tenant=True,
        person_id=LEGACY_OWNER_USER_ID,
    )


def test_a_participant_with_full_rights_is_not_the_owner():
    """Мутация: вернуть `preset_key == "owner"` — тест краснеет."""
    assert not _participant().is_owner, "участник с полными правами объявлен хозяином архива"
    assert _archive_owner().is_owner, "владелец перестал быть хозяином своего архива"


def test_a_personal_archive_keeps_the_preset_meaning():
    """Ошибка в другую сторону: в личном архиве хозяин определяется пресетом.

    Там человек один, `own_id` равен `user_id` у всех, и граница по учётке
    отобрала бы у единственного хозяина его же архив.
    """
    solo = ActorContext(user_id="alice", preset_key="owner", source="api-token")
    assert solo.is_owner
    guest = ActorContext(user_id="bob", preset_key="guest", source="api-token")
    assert not guest.is_owner


@pytest.mark.asyncio
async def test_a_participant_cannot_mint_a_token_for_the_owner(storage):
    """Токен — это ВСЯ власть учётки; выпустить его на чужую нельзя.

    Проверяется ДОРОГА целиком, а не отдельный инвариант. `require_delegable_account`
    здесь не помогает по построению: он сравнивает ОБЪЁМ прав, а у участника
    пресет тот же самый, значит «больше, чем у меня» не выполняется никогда.
    Личность защищает только `_protect_owner_target` — и защищала бы, если бы
    хозяином архива не считался каждый обладатель полного пресета.

    Мутация: вернуть `preset_key == "owner"` в `is_owner` — тест краснеет.
    """
    from fastapi import HTTPException

    from friday.admin_api._users import create_token

    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    storage.ensure_user(PARTICIPANT, preset_key="owner")
    storage.commit()
    auth = AuthorizationService(storage=storage)

    def _request(actor: ActorContext):
        request = type("Request", (), {})()
        request.app = type(
            "App", (), {"state": type("S", (), {"storage": storage, "auth_service": auth})()}
        )()
        request.state = type("RS", (), {"actor": actor, "json_body": {"user_id": LEGACY_OWNER_USER_ID}})()
        return request

    with pytest.raises(HTTPException) as denied:
        await create_token(_request(_participant()))
    assert denied.value.status_code == 403

    # Хозяину — можно: иначе владелец не заведёт себе второй токен.
    minted = await create_token(_request(_archive_owner()))
    assert minted["token"].startswith("jrc_")


def test_a_participant_cannot_hand_out_the_owner_preset(storage):
    """Раздача пресета владельца — это раздача хозяйского положения."""
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    storage.ensure_user(PARTICIPANT, preset_key="owner")
    storage.ensure_user("novice", preset_key="guest")
    storage.commit()
    auth = AuthorizationService(storage=storage)

    with pytest.raises(AuthorizationError):
        auth.set_user_preset("novice", "owner", acting_actor=_participant())
    assert auth.get_user_preset("novice") == "guest", "пресет всё-таки сменился"

    auth.set_user_preset("novice", "owner", acting_actor=_archive_owner())
    assert auth.get_user_preset("novice") == "owner"


def test_a_participant_cannot_touch_the_owner_account(storage):
    """Последний рубеж админских дорог: `_protect_owner_target`."""
    from fastapi import HTTPException

    from friday.admin_api._deps import _protect_owner_target

    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    storage.ensure_user(PARTICIPANT, preset_key="owner")
    storage.commit()
    auth = AuthorizationService(storage=storage)

    class _Request:
        def __init__(self, actor: ActorContext) -> None:
            self.app = type(
                "App", (), {"state": type("S", (), {"storage": storage, "auth_service": auth})()}
            )()
            self.state = type("RS", (), {"actor": actor})()

    with pytest.raises(HTTPException) as denied:
        _protect_owner_target(_Request(_participant()), LEGACY_OWNER_USER_ID)
    assert denied.value.status_code == 403

    _protect_owner_target(_Request(_archive_owner()), LEGACY_OWNER_USER_ID)
