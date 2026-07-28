"""Кем человек вошёл — не то же самое, чьи это данные.

До появления `user_identities` одно было равно другому: телеграм-аккаунт получал
идентификатор `telegram:{realm}:{id}`, и он же служил арендатором. Владелец,
импортировавший корпус через CLI, и он же, спрашивающий в телеграме, оказывались
РАЗНЫМИ арендаторами. Поиск ограничен арендатором, поэтому вопрос из телеграма
физически не мог найти собственные документы владельца — и система отвечала
«ничего не нашлось», будучи полностью права по своим правилам.

Заметить это по поведению почти невозможно: ответ выглядит как «в базе этого нет»,
а не как ошибка. На живой установке так и было — 66 поступлений под одним
аккаунтом, ноль знаний под тем, из которого задавали вопросы.

Три свойства, за которыми тут следят:

1. Связь работает: положили знание владельцу, спросили телеграмом — нашлось.
2. Связи нет — ничего не изменилось: чужой телеграм получает свой арендатор и
   чужих документов не видит. Привязка ПО УМОЛЧАНИЮ была бы выдачей доступа
   любому, кто написал боту.
3. Привязка не портит аккаунт, к которому привязались.
"""

from __future__ import annotations

import hashlib

import pytest

from jericho.storage.models import KnowledgeObject, RawObject, new_id

SECRET_FACT = "Договор аренды склада на Полевой подписан 14 марта, ставка 480 тысяч в месяц."


def _knowledge(storage, user_id: str, text: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="upload",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title="Аренда склада",
    )
    storage.store_knowledge_object(ko)
    return ko.id


# --- хранилище: сама связь ---------------------------------------------------


def test_an_unlinked_identity_resolves_to_nothing(storage):
    """«Связи нет» и «связь ведёт в аккаунт X» — разные ответы."""
    assert storage.resolve_identity("telegram", "100000001") is None


def test_a_linked_identity_names_its_account(storage):
    storage.ensure_user("owner", preset_key="owner")
    storage.link_identity("telegram", "100000001", "owner", linked_by="owner")
    assert storage.resolve_identity("telegram", "100000001") == "owner"
    assert storage.resolve_identity("telegram", "999") is None, "связь протекла на чужой номер"


def test_an_identity_belongs_to_exactly_one_account(storage):
    """Иначе «чьи это данные» перестаёт иметь ответ."""
    storage.ensure_user("owner")
    storage.ensure_user("other")
    storage.link_identity("telegram", "100000001", "owner")
    storage.link_identity("telegram", "100000001", "other")

    assert storage.resolve_identity("telegram", "100000001") == "other"
    assert len(storage.list_identities()) == 1, "перепривязка оставила вторую строку"


def test_linking_to_a_missing_account_is_refused(storage):
    with pytest.raises(ValueError, match="Unknown account"):
        storage.link_identity("telegram", "1", "nobody")


def test_unlinking_restores_the_separate_tenant(storage):
    storage.ensure_user("owner")
    storage.link_identity("telegram", "100000001", "owner")
    assert storage.unlink_identity("telegram", "100000001") is True
    assert storage.resolve_identity("telegram", "100000001") is None
    assert storage.unlink_identity("telegram", "100000001") is False


# --- сквозь HTTP: то, ради чего всё делалось --------------------------------


def _bridge_post(client, settings, *, chat_id: str, sender: str, text: str):
    """Подписанный запрос моста — тот же путь, которым ходит настоящий телеграм.

    Личность едет ЗАГОЛОВКАМИ и входит в подпись: подделать отправителя, не зная
    секрета моста, нельзя. Именно поэтому связь личность→арендатор безопасно
    строить по `X-Jericho-User`.
    """
    import json as _json
    import time
    import uuid

    from jericho.security import sign_bridge_request

    payload = _json.dumps(
        {"message": text, "telegram_user": {"first_name": "Владелец", "username": "owner"}}
    ).encode()
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    return client.post(
        "/api/chat",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Jericho-Timestamp": str(timestamp),
            "X-Jericho-User": sender,
            "X-Jericho-Chat": chat_id,
            "X-Jericho-Nonce": nonce,
            "X-Jericho-Signature": sign_bridge_request(
                settings.telegram_bridge_secret,
                timestamp=timestamp,
                method="POST",
                path="/api/chat",
                external_user_id=sender,
                chat_id=chat_id,
                nonce=nonce,
                body=payload,
            ),
        },
    )


@pytest.fixture
def bridged(settings):
    """Экземпляр с настроенным мостом и одним аккаунтом-владельцем с документом."""
    import dataclasses

    from fastapi.testclient import TestClient

    from jericho.server import create_app

    tuned = dataclasses.replace(
        settings,
        telegram_bridge_secret="s" * 48,
        telegram_allowed_chat_ids=[100000001],
    )
    app = create_app(tuned)
    with TestClient(app) as client:
        storage = app.state.storage
        owner_id = client.get(
            "/api/admin/users", headers={"Authorization": f"Bearer {tuned.api_token}"}
        ).json()["items"][0]["id"]
        _knowledge(storage, owner_id, SECRET_FACT)
        yield client, tuned, storage, owner_id


def test_without_a_link_the_question_cannot_reach_the_corpus(bridged):
    """Прежнее поведение, зафиксированное как факт, а не как норма.

    Именно так выглядела живая установка: документы под одним аккаунтом, вопросы —
    из другого, и «ничего не нашлось» было честным ответом не про то.
    """
    client, settings, storage, owner_id = bridged
    response = _bridge_post(
        client, settings, chat_id="100000001", sender="100000001", text="Что известно про аренду склада?"
    )
    assert response.status_code == 200, response.text
    answer = str(response.json().get("message") or "")

    assert storage.resolve_identity("telegram", "100000001") is None
    assert storage.count_knowledge_objects(owner_id) == 1
    assert "480" not in answer, "без связи ответ каким-то образом достал чужой корпус"

    # Спрашивавший — отдельный арендатор, и знаний у него нет. Это и есть то, что
    # на живой установке читалось как «в базе этого нет».
    asked_by = [u["id"] for u in storage.list_users(limit=50) if u["id"].startswith("telegram:")]
    assert asked_by, "телеграм-аккаунт не завёлся"
    assert storage.count_knowledge_objects(asked_by[0]) == 0


def test_after_linking_the_question_reaches_the_owners_corpus(bridged):
    """То самое свойство: импортировал одним путём — нашёл другим."""
    client, settings, storage, owner_id = bridged
    storage.link_identity("telegram", "100000001", owner_id, linked_by=owner_id)

    response = _bridge_post(
        client, settings, chat_id="100000001", sender="100000001", text="Что известно про аренду склада?"
    )
    assert response.status_code == 200, response.text
    answer = str(response.json().get("message") or "")

    # Утверждение — про САМ ОТВЕТ, а не про служебное поле: свойство, ради которого
    # всё делалось, звучит как «спросил в телеграме — получил из своего архива».
    assert "480" in answer and "Полевой" in answer, (
        f"факт из корпуса владельца не дошёл до ответа в телеграме: {answer[:200]!r}"
    )


def test_linking_does_not_turn_the_owner_into_a_telegram_account(bridged):
    """`ensure_user` переписал бы source и external_id аккаунта из канала входа."""
    client, settings, storage, owner_id = bridged
    before = storage.get_user(owner_id)
    storage.link_identity("telegram", "100000001", owner_id)

    _bridge_post(client, settings, chat_id="100000001", sender="100000001", text="привет")

    after = storage.get_user(owner_id)
    assert after["source"] == before["source"], "источник аккаунта переписан каналом входа"
    assert after["external_id"] == before["external_id"]
    assert after["preset_key"] == before["preset_key"]
    # А вот куда доставлять — это как раз то, что телеграм и должен сообщить.
    assert str(after["metadata_json"]).find("chat_id") >= 0


def test_somebody_elses_telegram_still_gets_its_own_tenant(bridged):
    """Привязка по умолчанию была бы выдачей архива каждому, кто написал боту."""
    client, settings, storage, owner_id = bridged
    storage.link_identity("telegram", "100000001", owner_id)

    response = _bridge_post(
        client, settings, chat_id="100000001", sender="555000111", text="Что известно про аренду склада?"
    )
    if response.status_code != 200:
        pytest.skip("чужой отправитель отклонён раньше — изоляция обеспечена другим слоем")
    assert response.json().get("user_id") != owner_id, "чужая личность обслужена арендатором владельца"
