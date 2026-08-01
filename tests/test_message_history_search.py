"""G16: search chat history (messages FTS), not only knowledge_objects.

Own history is findable; another user's is not. Empty query is a no-op.
DELETE keeps the external-content FTS index in sync (no orphan hits).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from fastapi.testclient import TestClient

from jericho.execution_kernel import ExecutionKernel
from jericho.ingestion import IngestionPipeline
from jericho.knowledge_graph import KnowledgeGraph
from jericho.permissions import AuthorizationService
from jericho.storage import init_storage
from jericho.web_surfer import WebSurfer
from tests.test_api_vertical_slice import _bridge_get, _bridge_request


def test_search_messages_finds_own_history_and_never_another_users(settings):
    storage = init_storage(settings)
    try:
        storage.ensure_user("alice")
        storage.ensure_user("bob")
        alice_conv = storage.create_conversation("alice", title="alice chat")
        bob_conv = storage.create_conversation("bob", title="bob chat")
        storage.store_message(
            alice_conv["id"],
            "alice",
            "user",
            "спрашивал про дежурства караула на прошлой неделе",
        )
        storage.store_message(
            bob_conv["id"],
            "bob",
            "user",
            "спрашивал про дежурства караула — это чужое",
        )

        own = storage.search_messages("alice", "дежурства")
        assert len(own) == 1
        assert own[0]["user_id"] == "alice"
        assert "караула" in own[0]["content"]

        # Even knowing bob's conversation id, alice must not see his messages.
        leak = storage.search_messages(
            "alice",
            "дежурства",
            conversation_id=bob_conv["id"],
        )
        assert leak == []

        bob_only = storage.search_messages("bob", "дежурства")
        assert len(bob_only) == 1
        assert bob_only[0]["user_id"] == "bob"
    finally:
        storage.close(final=True)


def test_search_messages_empty_query_is_safe(settings):
    storage = init_storage(settings)
    try:
        storage.ensure_user("alice")
        conv = storage.create_conversation("alice")
        storage.store_message(conv["id"], "alice", "user", "что-то важное")
        assert storage.search_messages("alice", "") == []
        assert storage.search_messages("alice", "   ") == []
    finally:
        storage.close(final=True)


def test_deleting_a_message_drops_it_from_messages_fts(settings):
    """Осиротевшая строка индекса после DELETE — это выдуманный ход в поиске.

    Проверяется САМ ИНДЕКС, а не выдача поиска: `search_messages` соединяет
    индекс с таблицей `messages`, и JOIN молча гасит осиротевшие строки. Прежняя
    редакция теста смотрела только на выдачу и оставалась зелёной при полностью
    удалённом триггере `messages_ad` — то есть охраняла не то, что обещает
    названием. Проверено исполнением: с `DROP TRIGGER messages_ad` тест проходил.

    Вторая половина — почему это не теория: rowid в SQLite переиспользуется.
    Осиротевшая строка индекса указывает на номер, который достанется СЛЕДУЮЩЕМУ
    сообщению, и тогда JOIN уже не спасает — чужой текст находится по слову из
    удалённого.

    Мутация: `DROP TRIGGER messages_ad` (или убрать его из схемы) — тест обязан
    покраснеть на счётчике индекса.
    """
    storage = init_storage(settings)
    try:
        storage.ensure_user("alice")
        conv = storage.create_conversation("alice")
        message = storage.store_message(
            conv["id"],
            "alice",
            "user",
            "уникальныймаркердежурств",
        )
        assert storage.search_messages("alice", "уникальныймаркердежурств")
        with storage.transaction() as conn:
            conn.execute("DELETE FROM messages WHERE id=?", (message["id"],))

        indexed = storage.execute(
            "SELECT COUNT(*) AS count FROM messages_fts WHERE messages_fts MATCH ?",
            ("уникальныймаркердежурств",),
        ).fetchone()["count"]
        assert indexed == 0, (
            f"в индексе осталось {indexed} строк удалённого сообщения — "
            "поиск скрывает их только JOIN'ом, до первого переиспользования rowid"
        )
        assert storage.search_messages("alice", "уникальныймаркердежурств") == []

        # Следующее сообщение получает освободившийся rowid: если бы строка
        # индекса пережила удаление, оно нашлось бы по чужому слову.
        storage.store_message(conv["id"], "alice", "user", "совершенно другой текст")
        assert storage.search_messages("alice", "уникальныймаркердежурств") == []
    finally:
        storage.close(final=True)


def test_http_me_messages_search_is_self_service_only(settings):
    from jericho.server import create_app

    scoped = replace(settings, telegram_allowed_chat_ids=[5001, 5002], telegram_owner_chat_ids=[])
    with TestClient(create_app(scoped)) as client:
        first = _bridge_request(
            client,
            scoped,
            "/api/chat",
            {
                "message": "про уникальныймаркердежурств alice",
                "source_ref": "telegram-update:hist1",
                "telegram_message_id": 1,
                "telegram_user": {"id": 5001},
            },
        )
        assert first.status_code == 200, first.text

        second = _bridge_request(
            client,
            scoped,
            "/api/chat",
            {
                "message": "про уникальныймаркердежурств bob",
                "source_ref": "telegram-update:hist2",
                "telegram_message_id": 1,
                "telegram_user": {"id": 5002},
            },
            user="5002",
            chat="5002",
        )
        assert second.status_code == 200, second.text

        alice_hit = _bridge_get(
            client,
            scoped,
            "/api/me/messages/search?q=%D1%83%D0%BD%D0%B8%D0%BA%D0%B0%D0%BB%D1%8C%D0%BD%D1%8B%D0%B9%D0%BC%D0%B0%D1%80%D0%BA%D0%B5%D1%80%D0%B4%D0%B5%D0%B6%D1%83%D1%80%D1%81%D1%82%D0%B2",
            user="5001",
            chat="5001",
        )
        assert alice_hit.status_code == 200, alice_hit.text
        body = alice_hit.json()
        assert body["count"] >= 1
        joined = " ".join(str(row.get("content") or "") for row in body["results"])
        assert "bob" not in joined

        empty = _bridge_get(
            client,
            scoped,
            "/api/me/messages/search?q=",
            user="5001",
            chat="5001",
        )
        assert empty.status_code == 200, empty.text
        assert empty.json()["count"] == 0
        assert empty.json()["results"] == []


def test_message_search_tool_scopes_to_actor(settings):
    storage = init_storage(settings)
    try:
        storage.ensure_user("alice", preset_key="user")
        storage.ensure_user("bob", preset_key="user")
        alice_conv = storage.create_conversation("alice")
        bob_conv = storage.create_conversation("bob")
        storage.store_message(alice_conv["id"], "alice", "user", "маркер_инструмента_alice")
        storage.store_message(bob_conv["id"], "bob", "user", "маркер_инструмента_bob")

        auth = AuthorizationService(storage)
        graph = KnowledgeGraph(storage)
        ingestion = IngestionPipeline(settings, storage, graph)
        web = WebSurfer(settings)
        kernel = ExecutionKernel(auth, settings)
        kernel.bind_services(storage, graph, web, ingestion)

        alice = auth.actor_for_user("alice", source="test")
        bob = auth.actor_for_user("bob", source="test")

        found = asyncio.run(kernel._message_search(actor=alice, query="маркер_инструмента"))  # noqa: SLF001
        assert found["count"] == 1
        assert "alice" in found["results"][0]["excerpt"]

        # Owner actor still only sees actor.user_id tenant — not foreign chats.
        storage.ensure_user("owner", preset_key="owner")
        owner = auth.actor_for_user("owner", source="test")
        owner_hits = asyncio.run(
            kernel._message_search(actor=owner, query="маркер_инструмента")  # noqa: SLF001
        )
        assert owner_hits["count"] == 0

        bob_found = asyncio.run(kernel._message_search(actor=bob, query="маркер_инструмента"))  # noqa: SLF001
        assert bob_found["count"] == 1
        assert "bob" in bob_found["results"][0]["excerpt"]
    finally:
        storage.close(final=True)
