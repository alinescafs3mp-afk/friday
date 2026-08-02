"""Какие права получает тот, кто написал впервые.

Владелец 2026-08-02 попросил: «все, кто первый раз написали, при создании
учётки — с правами админа», чтобы люди видели документы и записи друг друга.

Решение владельца, и ручка ровно под него — `FRIDAY_NEW_ACCOUNT_PRESET`. Цена
записана и здесь, и у настройки: вместе с открытой регистрацией это значит, что
ЛЮБОЙ человек, написавший боту, получает полный доступ к архиву и к
административным действиям. Поэтому проверяется не только «права выданы», но и
что владелец об этом узнаёт, и что опечатка в переменной окружения не раздаёт
права молча.
"""

from __future__ import annotations

import json
from dataclasses import replace

from fastapi.testclient import TestClient

from friday.server import create_app
from tests.test_api_vertical_slice import _signed_headers

_STRANGER = {"id": 998877, "first_name": "Незнакомец", "username": "stranger"}


def _write_first_message(client, settings, telegram_user=None) -> object:
    person = telegram_user or _STRANGER
    payload = {"message": "привет", "telegram_user": person}
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return client.post(
        "/api/chat",
        content=body,
        headers=_signed_headers(
            settings.telegram_bridge_secret,
            "POST",
            "/api/chat",
            body,
            str(person["id"]),
            str(person["id"]),
        ),
    )


def _preset_of(storage, external_id: str) -> str:
    row = storage.execute(
        "SELECT preset_key FROM users WHERE external_id=?", (external_id,)
    ).fetchone()
    return str(row["preset_key"]) if row else ""


def test_a_first_time_writer_gets_the_configured_preset(settings):
    """Мутация: игнорировать `new_account_preset` — тест краснеет."""
    tuned = replace(
        settings,
        telegram_open_registration=True,
        telegram_allowed_chat_ids=[],
        new_account_preset="owner",
    )
    app = create_app(tuned)
    with TestClient(app) as client:
        response = _write_first_message(client, tuned)
        assert response.status_code == 200, response.text
        assert _preset_of(app.state.storage, "998877") == "owner", (
            "первый написавший не получил настроенные права"
        )


def test_without_the_setting_the_narrow_preset_stays(settings):
    """Контроль: умолчание не изменилось — с улицы приходит `newcomer`."""
    tuned = replace(
        settings,
        telegram_open_registration=True,
        telegram_allowed_chat_ids=[],
        new_account_preset="",
    )
    app = create_app(tuned)
    with TestClient(app) as client:
        assert _write_first_message(client, tuned).status_code == 200
        assert _preset_of(app.state.storage, "998877") == "newcomer"


def test_a_typo_in_the_setting_never_grants_anything(settings):
    """Мутация: убрать проверку `preset_exists` — учётка ляжет с несуществующим
    пресетом, и права окажутся какими угодно.

    Опечатка в переменной окружения не должна ни закрывать вход, ни раздавать
    права молча: человек заводится по прежнему правилу, а расхождение пишется в
    журнал ошибкой.
    """
    tuned = replace(
        settings,
        telegram_open_registration=True,
        telegram_allowed_chat_ids=[],
        new_account_preset="ownerr",
    )
    app = create_app(tuned)
    with TestClient(app) as client:
        assert _write_first_message(client, tuned).status_code == 200
        assert _preset_of(app.state.storage, "998877") == "newcomer"


def test_the_owner_is_told_about_every_new_account_and_about_full_rights(settings):
    """Мутация: вернуть условие `preset == "newcomer"` — уведомление пропадёт.

    Прежнее условие было верно, пока автоматически выдавался единственный узкий
    пресет. С этой настройкой человек с улицы может получить полные права — и
    именно об этом владельцу знать важнее всего, а он бы не узнал ничего.
    """
    tuned = replace(
        settings,
        telegram_open_registration=True,
        telegram_allowed_chat_ids=[],
        telegram_owner_chat_ids=[467035772],
        new_account_preset="owner",
    )
    app = create_app(tuned)
    with TestClient(app) as client:
        assert _write_first_message(client, tuned).status_code == 200
        queued = app.state.storage.execute(
            "SELECT body FROM outbound_notifications WHERE body LIKE '%самозарегистрировался%'"
        ).fetchall()
        assert queued, "владельцу не сказали о новой учётке"
        body = str(queued[0]["body"])
        assert "Незнакомец" in body and "@stranger" in body
        assert "preset owner" in body
        assert "ПОЛНЫЕ права" in body, "владелец не предупреждён о полном доступе"


def test_an_allowlisted_chat_is_not_reported_as_a_stranger(settings):
    """Контроль: свои по списку — не самозарегистрировавшиеся."""
    tuned = replace(
        settings,
        telegram_open_registration=True,
        telegram_allowed_chat_ids=[998877],
        telegram_owner_chat_ids=[467035772],
        new_account_preset="owner",
    )
    app = create_app(tuned)
    with TestClient(app) as client:
        assert _write_first_message(client, tuned).status_code == 200
        queued = app.state.storage.execute(
            "SELECT body FROM outbound_notifications WHERE body LIKE '%самозарегистрировался%'"
        ).fetchall()
        assert not queued
        # Настройка действует и для чатов из списка: она про ЛЮБУЮ новую учётку.
        assert _preset_of(app.state.storage, "998877") == "owner"
