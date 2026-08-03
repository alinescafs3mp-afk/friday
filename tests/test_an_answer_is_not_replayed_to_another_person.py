"""Кэш ответа не превращает личный разговор в общий.

Разбор Codex (`sol/HARDENING_FOR_OPUS.md`, §12.3). `POST /api/chat` заявляет и
завершает ключ идемпотентности по `(actor.user_id, source_ref)`, а для API
`source_ref` задаёт КЛИЕНТ. В общем архиве `actor.user_id` — арендатор, один на
всех, поэтому два участника с одинаковым `source_ref` попадают в один namespace:
второй получает `status=replay` и ЧУЖОЙ ответ вместе с чужим `conversation_id`.

Документы у них общие намеренно — в этом весь смысл общего архива. Ответы и
разговоры личные, и кэш не имеет права превращать одно в другое.

Пятый и последний случай семейства «`user_id` больше не человек»:

    заявка была видна и решаема любым участником (§12.2);
    указание «отвечай мне кратко» ложилось в общую учётку;
    личный запрет не действовал (§12.1);
    личный лимит обращений был общим (§12.4);
    ответ отдавался чужому по совпавшему ключу — здесь.

Повтор ОДНОГО человека при этом обязан остаться идемпотентным: он для того и
сделан — мост перепосылает сообщение при обрыве связи, и второй ход не должен
рождать второй ответ.
"""

from __future__ import annotations

import hashlib

import pytest

from friday.permissions import ActorContext


def _person(name: str) -> ActorContext:
    return ActorContext(
        user_id="tenant",
        preset_key="user",
        source="api-token",
        shared_tenant=True,
        person_id=name,
    )


@pytest.fixture
def shared(storage):
    for name in ("tenant", "person-a", "person-b"):
        storage.ensure_user(name)
    return storage


SAME_HASH = hashlib.sha256("одно и то же тело запроса".encode()).hexdigest()


def test_the_collision_this_prevents(shared) -> None:
    """ПОКАЗЫВАЕТ цену ошибки, а не ловит её. Роль этого теста — объяснение.

    Хранилище исправно: по разным идентификаторам ключи не сталкиваются. Ошибка
    была в МАРШРУТЕ — он подставлял туда арендатора. Здесь воспроизведено, что
    происходило: один и тот же идентификатор для двоих отдаёт второму чужой
    ответ вместе с чужим `conversation_id`.

    Сторожат правку тесты ниже, проверяющие вызовы. Выдавать этот за сторожа
    было бы обманом: он зелёный и на сломанном коде.
    """
    tenant = "tenant"
    first = shared.idempotency_claim(tenant, "ref-1", request_hash=SAME_HASH, lease_seconds=60)
    shared.idempotency_complete(
        tenant, "ref-1", first["lease_token"], {"conversation_id": "conv-a"}
    )

    second = shared.idempotency_claim(tenant, "ref-1", request_hash=SAME_HASH, lease_seconds=60)

    assert second["status"] == "replay"
    assert (second.get("response") or {}).get("conversation_id") == "conv-a", (
        "именно это и получал второй участник: чужой разговор"
    )


def test_different_people_do_not_collide(shared) -> None:
    """Договор хранилища: разные идентификаторы — разные ключи."""
    a = shared.idempotency_claim("person-a", "ref-3", request_hash=SAME_HASH, lease_seconds=60)
    shared.idempotency_complete("person-a", "ref-3", a["lease_token"], {"conversation_id": "conv-a"})

    b = shared.idempotency_claim("person-b", "ref-3", request_hash=SAME_HASH, lease_seconds=60)

    assert b["status"] == "acquired"


def test_a_repeat_by_the_same_person_is_still_idempotent(shared) -> None:
    """Обратная сторона, ради которой механизм и существует.

    Мост перепосылает сообщение при обрыве связи; второй ход не должен рождать
    второй ответ. Правка, разводящая ключи, не имеет права это сломать.
    """
    who = _person("person-a").own_id
    first = shared.idempotency_claim(who, "ref-2", request_hash=SAME_HASH, lease_seconds=60)
    shared.idempotency_complete(who, "ref-2", first["lease_token"], {"conversation_id": "conv-a"})

    again = shared.idempotency_claim(who, "ref-2", request_hash=SAME_HASH, lease_seconds=60)

    assert again["status"] == "replay"
    assert (again.get("response") or {}).get("conversation_id") == "conv-a"


def test_the_route_claims_by_the_person(monkeypatch) -> None:
    """Механизм без правильного вызова работой не является.

    Проверяется, что маршрут передаёт в ключ ЧЕЛОВЕКА. Осмотр исходника здесь
    уместен: разница видна ровно в одном имени, а поднимать ради него полное
    приложение с двумя учётками дороже, чем полученная уверенность.
    """
    import inspect

    from friday.server import create_app

    source = inspect.getsource(create_app)
    at = source.index("idempotency_claim(")
    window = source[at : at + 700]
    assert "actor.own_id" in window, "ключ идемпотентности чата заявляется по арендатору"
    assert "actor.user_id" not in window, "в ключе остался арендатор"


def test_every_idempotency_call_uses_the_person() -> None:
    """Заявить по человеку и завершить по арендатору — хуже, чем не чинить вовсе.

    Ключ остался бы висеть до истечения срока: заявка есть, а завершения по этому
    же идентификатору нет. Поэтому проверяются ВСЕ четыре вызова, а не только
    первый.
    """
    import inspect

    from friday.server import create_app

    source = inspect.getsource(create_app)
    for name in ("idempotency_claim", "idempotency_complete", "idempotency_release", "idempotency_renew"):
        # Искать надо ВЫЗОВ, а не упоминание: слово встречается и в комментарии
        # («гонка двух /regenerate закрыта idempotency_claim…»), и первая редакция
        # этой проверки на нём и спотыкалась. Окно широкое: перед идентификатором
        # у двух вызовов стоит объясняющий комментарий на четыре строки.
        for at in _positions(source, f"{name}("):
            window = source[at : at + 700]
            assert "actor.own_id" in window, f"{name} зовётся не по человеку"


def _positions(text: str, needle: str) -> list[int]:
    found: list[int] = []
    start = text.find(needle)
    while start >= 0:
        found.append(start)
        start = text.find(needle, start + 1)
    return found
