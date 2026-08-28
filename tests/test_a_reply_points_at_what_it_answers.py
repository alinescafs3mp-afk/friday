"""Ответ на реплику доходит до модели и НЕ задевает классификатор.

`_process_update` не читал `reply_to_message` вовсе: человек отвечал на конкретное
сообщение — своё или Пятницы, — и связь терялась. Модель видела «а подробнее?» без
того, к чему это относится.

Ловушка, из-за которой цитата едет ОТДЕЛЬНЫМ полем, а не приклеивается к тексту.
Текст хода идёт (а) в архив как слова человека и (б) в `is_relational_query`,
который решает, включать ли графовое расширение. Реляционная фраза внутри ЦИТАТЫ
включила бы граф ходу, который об этом не просил, — ровно та ошибка, которую уже
ловили состязательным ревью на `_contextualize_query` и чинили проверкой
`message`, а не `search_query`.

"""

from __future__ import annotations

from typing import Any

import pytest

from friday.telegram_bridge import TelegramBridge


def test_the_bridge_reads_what_the_person_replied_to():
    """Мутация: вернуть `_reply_quote` пустую строку — краснеет."""

    message = {
        "text": "а подробнее?",
        "reply_to_message": {"text": "Отчёт сдан 3 марта, подписал Иванов."},
    }
    assert TelegramBridge._reply_quote(message) == "Отчёт сдан 3 марта, подписал Иванов."


def test_a_reply_to_a_picture_uses_its_caption():
    """Ответить можно и на картинку: текста у неё нет, подпись есть."""

    message = {"text": "что это?", "reply_to_message": {"caption": "Схема участка"}}
    assert TelegramBridge._reply_quote(message) == "Схема участка"


def test_an_ordinary_message_quotes_nothing():
    assert TelegramBridge._reply_quote({"text": "привет"}) == ""
    assert TelegramBridge._reply_quote({"text": "привет", "reply_to_message": {}}) == ""


def test_a_long_quote_is_bounded_at_the_source():
    """Отвечают и на документ в тысячу строк, а смысл только в том, НА ЧТО показали.

    Мутация: снять ограничение длины — краснеет."""

    message = {"text": "и?", "reply_to_message": {"text": "я" * 5000}}
    assert len(TelegramBridge._reply_quote(message)) == 1000


@pytest.mark.asyncio
async def test_the_quote_reaches_the_model_without_touching_the_classifier(settings, storage):
    """Главная проба этого файла.

    Цитата обязана дойти до контекста модели и НЕ повлиять на решение о графе:
    в цитате стоит реляционная фраза («с кем работал»), а сам вопрос человека —
    обычный. Граф включаться не должен.

    Мутация: приклеить цитату к тексту хода — краснеет, потому что классификатор
    увидит чужие слова как слова человека.
    """
    from friday.agent_runtime import AgentRuntime
    from friday.permissions import ActorContext

    seen: dict[str, Any] = {}

    class _Searcher:
        async def search(self, _user_id: str, _query: str, **kwargs: Any) -> dict[str, Any]:
            seen["graph_expansion"] = kwargs.get("graph_expansion")
            return {"results": [], "count": 0, "trace": []}

    storage.ensure_user("alice")
    agent = AgentRuntime(settings, storage)
    actor = ActorContext(user_id="alice", preset_key="owner", source="api")

    result = await agent.chat(
        "alice",
        "покажи отчёт за март",
        actor=actor,
        hybrid_searcher=_Searcher(),
        kg=object(),
        reply_to="а с кем работал Иванов в том проекте",
    )

    assert result, "ход не дал ответа — проба проверяет не то"
    assert seen.get("graph_expansion") is False, (
        "реляционная фраза из ЦИТАТЫ включила графовое расширение: цитата дошла "
        "до классификатора, хотя человек просил другое"
    )

    stored = storage.get_conversation_messages(storage.list_conversations("alice")[0]["id"], user_id="alice")
    user_lines = [row["content"] for row in stored if row["role"] == "user"]
    assert user_lines, "ход человека не сохранён"
    assert "с кем работал" not in user_lines[0], (
        "цитата попала в архив как слова человека — он этого не писал"
    )


def test_the_quote_actually_reaches_the_model_context(settings, storage):
    """Без этой пробы вся правка могла бы быть пустышкой.

    Предыдущая проба доказывает, что цитата НЕ задела классификатор и НЕ попала в
    архив. Но «не навредила» и «доехала» — разные утверждения, и первое без
    второго выполняется у пустой строки тоже.

    Мутация: не класть `reply_quote` в контекст модели — краснеет.
    """
    import json

    from friday.agent_runtime import AgentContext, AgentRuntime

    storage.ensure_user("alice")
    agent = AgentRuntime(settings, storage)
    context = AgentContext(conversation_id="conv-1", user_id="alice")
    context.reply_quote = "Отчёт сдан 3 марта, подписал Иванов."

    messages = agent._build_initial_messages(context, "а подробнее?", None, tool_enabled=False)
    rendered = json.dumps(messages, ensure_ascii=False)

    assert "Отчёт сдан 3 марта" in rendered, "цитата не доехала до контекста модели"
    assert "reply_quote" in rendered, "цитата доехала без имени поля — модель не поймёт, что это"


@pytest.mark.asyncio
async def test_the_bridge_puts_the_quote_into_the_request(tmp_path):
    """Проводочная проба: смотрит, что мост ОТПРАВИЛ, а не что умеет вырезать.

    Без неё правка держалась на `_reply_quote`, который никто не звал: помощник,
    умеющий достать цитату, и запрос, её несущий, — разные утверждения. Мутация
    «мост не кладёт цитату в запрос» первую редакцию пробы ПЕРЕЖИЛА.
    """
    from friday.telegram_bridge import TelegramBridge, TelegramConfig

    class _Telegram:
        async def post(self, url: str, **kwargs: Any):
            import httpx

            return httpx.Response(200, json={"ok": True, "result": {}}, request=httpx.Request("POST", url))

    class _Backend:
        def __init__(self) -> None:
            self.bodies: list[dict[str, Any]] = []

        async def request(self, method: str, url: str, **kwargs: Any):
            import json as _json

            import httpx

            body = kwargs.get("content")
            if body:
                self.bodies.append(_json.loads(body))
            return httpx.Response(200, json={"message": "Готово"}, request=httpx.Request(method, url))

        async def post(self, url: str, **kwargs: Any):
            return await self.request("POST", url, **kwargs)

    bridge = TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )
    telegram, backend = _Telegram(), _Backend()
    update = {
        "update_id": 900,
        "message": {
            "message_id": 11,
            "chat": {"id": 5001},
            "from": {"id": 1001, "first_name": "Alice"},
            "text": "а подробнее?",
            "reply_to_message": {"message_id": 10, "text": "Отчёт сдан 3 марта."},
        },
    }
    try:
        await bridge._process_update(telegram, backend, update, cached_response=None)
    finally:
        bridge._inbox.close()

    chat_bodies = [body for body in backend.bodies if "message" in body]
    assert chat_bodies, f"запроса к /api/chat не было: {backend.bodies}"
    assert chat_bodies[-1].get("reply_to") == "Отчёт сдан 3 марта.", (
        f"цитата не уехала в запрос: {sorted(chat_bodies[-1])}"
    )
    assert chat_bodies[-1]["message"] == "а подробнее?", (
        "цитата приклеилась к тексту хода — она обязана ехать отдельным полем"
    )
