"""Общий архив: документы и записи — одни на всех, переписка — своя у каждого.

Владелец 2026-08-02: «надо не только чтобы они видели документы друг друга, но и
чтобы могли с ними взаимодействовать».

Одних админских прав для этого мало — замерено на стенде: с полным набором прав
человек видит чужое через админские МАРШРУТЫ, а обычный разговор ищет только в
своём арендаторе и на вопрос про чужую смету дал ноль попаданий, уйдя в интернет.
Поэтому снимается сама изоляция: все работают в одном арендаторе.

Граница проведена сознательно и проверяется здесь же: общими становятся
документы, знания и «Входящие» — то, о чём просьба. Личная переписка общей НЕ
становится, иначе любой сотрудник читал бы разговоры владельца; первая редакция
правки именно это и сделала — обычный пользователь видел все двести разговоров.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID, AuthorizationService
from friday.server import create_app


@pytest.fixture
def shared(settings):
    return replace(settings, shared_archive=True)


def _person(client, owner_headers, user_id: str) -> dict[str, str]:
    client.post("/api/admin/users", json={"id": user_id, "preset_key": "user"}, headers=owner_headers)
    token = client.post("/api/admin/tokens", json={"user_id": user_id}, headers=owner_headers)
    return {"Authorization": f"Bearer {token.json()['token']}"}


def test_the_actor_carries_the_tenant_and_the_person_apart(storage):
    """Мутация: вернуть `user_id` человека — общего архива не будет.

    Подмена делается в одном месте, а не в двухстах сорока трёх, где спрашивают
    `actor.user_id`: одно забытое место означало бы, что часть системы видит
    общий корпус, а часть — свой, и расхождение вылезло бы молча.
    """
    storage.ensure_user("kolya", preset_key="user")
    shared_service = AuthorizationService(storage, shared_tenant=LEGACY_OWNER_USER_ID)
    actor = shared_service.actor_for_user("kolya", source="test")
    assert actor.user_id == LEGACY_OWNER_USER_ID, "арендатор не стал общим"
    assert actor.own_id == "kolya", "человек потерялся"
    assert actor.shared_tenant is True
    assert actor.preset_key == "user", "права должны остаться личными"

    ordinary = AuthorizationService(storage).actor_for_user("kolya", source="test")
    assert ordinary.user_id == "kolya"
    assert ordinary.own_id == "kolya"
    assert ordinary.shared_tenant is False


def test_a_linked_identity_outside_the_shared_mode_keeps_one_identifier(storage):
    """Мутация: выводить признак из «identity_id != user_id» — тест краснеет.

    Связанная личность бывает и при обычной настройке — владелец, вошедший через
    бота. Вывод признака из неравенства разносил его переписку по двум разным
    идентификаторам: поймано двадцатью пятью упавшими тестами.
    """
    storage.ensure_user("owner-person", preset_key="owner")
    actor = AuthorizationService(storage).actor_for_user(
        "owner-person", source="telegram-bridge", identity_id="telegram:12345"
    )
    assert actor.shared_tenant is False
    assert actor.own_id == "owner-person", "переписка уехала бы в другой идентификатор"


def test_one_person_finds_and_edits_what_another_wrote(shared):
    """Главная проверка просьбы: увидеть И взаимодействовать."""
    app = create_app(shared)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {shared.api_token}"}
        kolya = _person(client, owner, "kolya")
        vasya = _person(client, owner, "vasya")

        # Знание кладётся тем же путём, каким его кладёт разговор: через приём
        # текста от лица Коли. Отдельного POST /api/knowledge в контракте нет.
        import asyncio

        from friday.ingestion import IngestionPipeline
        from friday.knowledge_graph import KnowledgeGraph

        storage = app.state.storage
        pipe = IngestionPipeline(shared, storage, KnowledgeGraph(storage))
        asyncio.run(
            pipe.ingest_text(
                LEGACY_OWNER_USER_ID,
                "Акт сверки №91. Сумма 118 000 рублей, подписал Коля.",
                force_knowledge=True,
                source_ref="shared-akt-91",
            )
        )

        listing = client.get("/api/knowledge?limit=10", headers=vasya)
        assert listing.status_code == 200
        items = listing.json()["items"]
        mine = [item for item in items if "91" in str(item.get("title"))]
        assert mine, f"чужой документ не виден: {[i.get('title') for i in items][:5]}"
        knowledge_id = mine[0]["id"]
        edited = client.patch(
            f"/api/knowledge/{knowledge_id}",
            json={"summary": "Проверено Васей"},
            headers=vasya,
        )
        assert edited.status_code == 200, "чужой документ нельзя править"

        again = client.get(f"/api/knowledge/{knowledge_id}", headers=kolya)
        assert again.status_code == 200
        body = again.json()
        payload = body.get("item") if isinstance(body.get("item"), dict) else body
        assert "Васей" in str(payload.get("summary") or ""), "правка не дошла до автора"


def test_the_correspondence_stays_private(shared):
    """Мутация: убрать `own_id` из маршрутов разговоров — тест краснеет.

    Первая редакция делала общими и разговоры: обычный пользователь видел все
    двести, включая переписку владельца. Просьба была про документы и записи.
    """
    app = create_app(shared)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {shared.api_token}"}
        kolya = _person(client, owner, "kolya")
        vasya = _person(client, owner, "vasya")

        storage = app.state.storage
        storage.create_conversation("kolya", title="Личный разговор Коли")

        theirs = client.get("/api/conversations", headers=vasya).json()
        titles = [str(item.get("title")) for item in theirs.get("items") or []]
        assert "Личный разговор Коли" not in titles, "чужая переписка видна"

        mine = client.get("/api/conversations", headers=kolya).json()
        assert "Личный разговор Коли" in [
            str(item.get("title")) for item in mine.get("items") or []
        ], "человек потерял собственную переписку"


def test_without_the_setting_isolation_is_intact(settings):
    """Контроль: умолчание не изменилось — арендаторы по-прежнему разделены."""
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        kolya = _person(client, owner, "kolya")
        vasya = _person(client, owner, "vasya")
        client.post(
            "/api/knowledge",
            json={"title": "Только для Коли", "content": "секрет"},
            headers=kolya,
        )
        listing = client.get("/api/knowledge?limit=10", headers=vasya).json()
        assert [item for item in listing["items"] if item["title"] == "Только для Коли"] == []


def test_the_trail_still_says_who_acted(shared, storage):
    """Мутация: писать в аудит `actor.user_id` — след станет «кто-то из нас».

    В общем архиве арендатор у всех один; кто действовал, видно только по
    личному идентификатору.
    """
    import asyncio

    from friday.execution_kernel import ExecutionKernel
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph
    from friday.web_surfer import WebSurfer

    storage.ensure_user("kolya", preset_key="user")
    auth = AuthorizationService(storage, shared_tenant=LEGACY_OWNER_USER_ID)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(auth, shared)
    kernel.bind_services(storage, graph, WebSurfer(shared), IngestionPipeline(shared, storage, graph))
    actor = auth.actor_for_user("kolya", source="test")

    asyncio.run(kernel.execute("kg_stats", {}, actor=actor))

    rows = storage.execute(
        "SELECT user_id, after_json FROM audit_log WHERE action='tool.invoke' ORDER BY created_at DESC LIMIT 1"
    ).fetchall()
    assert rows, "след не записан"
    assert str(rows[0]["user_id"]) == "kolya", "в следе арендатор вместо человека"
    assert LEGACY_OWNER_USER_ID in str(rows[0]["after_json"]), "потерян арендатор, в котором шла работа"


def test_a_telegram_turn_survives_the_shared_archive(shared):
    """Мутация: вернуть `actor.user_id` в привязку канала — тест краснеет.

    Замерено на живом мосте: `/api/chat` отвечал 500 «Conversation does not
    belong to user», мост откладывал сообщение и крутил его по кругу, а человек
    не получал НИЧЕГО — при том что ответ был сформирован и лежал в базе,
    владелец видел его в админке.

    Причина — в общем архиве `user_id` у всех один, разговор создаётся под
    личным идентификатором, и привязка канала к разговору не проходила проверку
    принадлежности.
    """
    import inspect

    from friday import server

    source = inspect.getsource(server)
    assert "get_channel_session(\n                actor.own_id" in source or (
        "actor.own_id,\n                \"telegram\"," in source
    ), "сессия канала ищется по арендатору, а не по человеку"
    # И привязка, и чтение — обе стороны, иначе ход рвётся на второй реплике.
    bindings = source.count('set_channel_conversation(\n                    actor.own_id')
    assert bindings == 2, f"привязок канала на личный идентификатор: {bindings}, ожидалось 2"


def test_the_channel_session_round_trips_for_a_person(shared, storage):
    """Поведением: привязали разговор к чату — нашли его обратно."""
    storage.ensure_user("kolya", preset_key="user")
    conversation = storage.create_conversation("kolya", title="Из телеграма")
    storage.set_channel_conversation("kolya", "telegram", "5001", conversation["id"], mode="dialogue")
    session = storage.get_channel_session("kolya", "telegram", "5001")
    assert session and str(session["conversation_id"]) == conversation["id"]
