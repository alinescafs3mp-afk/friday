"""Сбор контекста включает граф только для измеренного реляционного режима.

Путь, который здесь защищается, главный: `_prepare_context` собирает контекст на каждое
сообщение, то есть через него проходит каждый вопрос владельца в Telegram. Долгое время
считалось, что граф там не участвует вовсе — это верно только про ИНСТРУМЕНТ
`memory_search`; автоматический сбор контекста получал `kg` от `server.py` и расширялся
по графу всегда.

Замер на золотом наборе живого архива (20 эталонов, три руки на одном коде, критерий
объявлен до запуска — чистый выигрыш не меньше 2 кейсов):

    kg + расширение     recall@10 0.1500  MRR 0.0813   <- было в бою
    kg без расширения   recall@10 0.3500  MRR 0.1530   <- стало
    без kg вовсе        recall@10 0.3500  MRR 0.1530

Расширение уполовинивало качество обычного поиска. Отдельный замер на 12 заранее
размеченных реляционных кейсах дал net_gain=2 без сбоев, поэтому граф включается
только по реляционному классификатору. `kg` остаётся и при выключенном расширении:
упомянутые сущности для контекста достаются бесплатно.

Тест ПРОВОДОЧНЫЙ. Проверяется не то, что механизм умеет выключаться (это дело
`retrieval`), а то, что боевой путь его действительно выключает. В этом проекте
зелёный юнит-тест на неподключённом механизме ловили многократно.
"""

from __future__ import annotations

import pytest


class _SpySearcher:
    """Запоминает, с чем боевой код позвал поиск."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def search(self, user_id, query, **kwargs):
        self.calls.append({"user_id": user_id, "query": query, **kwargs})
        return {"results": [], "entity_matches": [], "strategy": {}}


@pytest.mark.asyncio
async def test_context_retrieval_asks_for_no_graph_expansion(settings, storage):
    """Мутация, которую тест обязан ловить: убрать `graph_expansion=False` из вызова
    в `_prepare_context`. Значение по умолчанию — True, то есть дефект вернётся молча
    и проявится только уполовиненным recall, который никто не заметит без набора."""
    from jericho.agent_runtime import AgentRuntime
    from jericho.knowledge_graph import KnowledgeGraph

    storage.ensure_user("alice")
    agent = AgentRuntime(settings, storage)
    spy = _SpySearcher()

    await agent._prepare_context(
        "alice",
        "что известно про склад",
        "conv-test",
        prior_history=[],
        kg=KnowledgeGraph(storage),
        searcher=spy,
        ingestion_result=None,
        interaction_mode="knowledge_work",
    )

    assert spy.calls, "боевой путь вообще не позвал поиск — проба проверяет не то"
    call = spy.calls[0]
    assert call.get("kg") is not None, (
        "граф должен ОСТАВАТЬСЯ ради упомянутых сущностей: убрать его целиком — "
        "другая правка, и она теряет entity_matches"
    )
    assert call.get("graph_expansion") is False, (
        "расширение по графу вернулось в путь агента: замерено, что оно уполовинивает "
        "recall@10 (0.3500 -> 0.1500)"
    )


@pytest.mark.asyncio
async def test_context_retrieval_expands_graph_for_measured_relational_form(settings, storage):
    """Мутация: заменить mode-dependent флаг на False — этот тест обязан упасть."""
    from jericho.agent_runtime import AgentRuntime
    from jericho.knowledge_graph import KnowledgeGraph

    storage.ensure_user("alice")
    agent = AgentRuntime(settings, storage)
    spy = _SpySearcher()

    await agent._prepare_context(
        "alice",
        "с кем работал Альфа",
        "conv-test",
        prior_history=[],
        kg=KnowledgeGraph(storage),
        searcher=spy,
        ingestion_result=None,
        interaction_mode="knowledge_work",
    )

    assert spy.calls
    assert spy.calls[0].get("graph_expansion") is True


@pytest.mark.asyncio
async def test_a_relational_previous_turn_does_not_leak_into_an_unrelated_follow_up(settings, storage):
    """Found by adversarial review: `_contextualize_query` joins a short
    pronoun/when-where-why follow-up with the PREVIOUS user turn for retrieval
    (`f"{clean}\\n{previous[:500]}"`) — but `is_relational_query` used to run
    on that JOINED string, so a relational phrase in the prior turn alone
    turned graph_expansion on for a current turn asking something unrelated.

    "А когда это было?" is a plain temporal follow-up and is not relational on
    its own; the previous turn asked "с кем работал" (relational). The joined
    search_query IS relational-shaped, but the actual current question is not,
    and graph_expansion must follow what was asked NOW.

    Мутация: вернуть `is_relational_query(search_query)` вместо `(message)` —
    тест обязан покраснеть.
    """
    from jericho.agent_runtime import AgentRuntime
    from jericho.knowledge_graph import KnowledgeGraph

    storage.ensure_user("alice")
    agent = AgentRuntime(settings, storage)
    spy = _SpySearcher()

    history = [{"role": "user", "content": "С кем работал Иван Петров над проектом Аврора?"}]
    await agent._prepare_context(
        "alice",
        "А когда это было?",
        "conv-test",
        prior_history=history,
        kg=KnowledgeGraph(storage),
        searcher=spy,
        ingestion_result=None,
        interaction_mode="dialogue",
    )

    assert spy.calls
    # The joined search_query DOES carry the previous turn's relational phrase
    # (retrieval still benefits from it) — the bug was letting THAT decide
    # graph_expansion instead of the current message alone.
    assert "\n" in spy.calls[0].get("query", ""), "проба проверяет не тот сценарий — follow-up не склеился"
    assert spy.calls[0].get("graph_expansion") is False, (
        "реляционная фраза из ПРЕДЫДУЩЕГО хода включила граф для текущего вопроса, "
        "который сам по себе не о связях"
    )


@pytest.mark.asyncio
async def test_generated_document_notice_never_reaches_relational_classifier(settings, storage, monkeypatch):
    """A filename in a backend-generated notice is data, not the user's query.

    Mutation: remove the synthetic-system-notice guard in ``_prepare_context``.
    The fail-fast classifier below is then called and this test fails.
    """
    import jericho.agent_runtime as agent_runtime
    from jericho.agent_runtime import AgentRuntime
    from jericho.knowledge_graph import KnowledgeGraph

    def reject_generated_notice(query: str) -> bool:
        raise AssertionError(f"generated document notice reached relational classifier: {query!r}")

    monkeypatch.setattr(agent_runtime, "is_relational_query", reject_generated_notice)
    storage.ensure_user("alice")
    agent = AgentRuntime(settings, storage)
    spy = _SpySearcher()
    message = "Загружен документ: с кем работал иван отчет.txt"

    await agent._prepare_context(
        "alice",
        message,
        "conv-test",
        prior_history=[],
        kg=KnowledgeGraph(storage),
        searcher=spy,
        ingestion_result=None,
        synthetic_document_notice=True,
        interaction_mode="dialogue",
    )

    assert spy.calls, "боевой путь вообще не позвал поиск — проба проверяет не то"
    assert spy.calls[0]["query"] == message
    assert spy.calls[0]["graph_expansion"] is False


@pytest.mark.asyncio
async def test_a_spoken_question_is_a_human_turn_not_a_generated_notice(settings, monkeypatch, tmp_path):
    """Сказанное вслух — слова человека, а не строка, сочинённая backend'ом.

    Флаг `synthetic_document_notice` нёс сразу два факта: «текст сгенерирован
    системой» и «файл уже принят отдельно». Для голосового сообщения верен только
    второй — транскрипт это вопрос человека. Пока флаг был один, вопрос «с кем
    работал Иван», заданный ГОЛОСОМ, объявлялся системным уведомлением и терял
    графовое расширение, которое тот же вопрос, набранный руками, получает
    (замерено: net_gain=2 на 12 реляционных кейсах).

    Проверяется боевой путь `/api/chat` целиком, а не помощник: дефект был именно
    в проводке, юнит-тест на классификаторе его не видел.

    Мутация: вернуть `synthetic_document_notice = True` для голоса — тест обязан
    покраснеть.
    """
    from fastapi.testclient import TestClient

    from jericho.server import create_app

    app = create_app(settings)
    captured: dict[str, object] = {}

    with TestClient(app) as client:
        real_chat = app.state.agent.chat

        async def _spy_chat(user_id, message, **kwargs):
            captured["message"] = message
            captured["synthetic"] = kwargs.get("synthetic_document_notice")
            return await real_chat(user_id, message, **kwargs)

        monkeypatch.setattr(app.state.agent, "chat", _spy_chat)

        async def _fake_ingest_file(*_args, **_kwargs):
            return {
                "transcript_text": "С кем работал Иван Петров над проектом Аврора?",
                "queued_for_review": True,
                "knowledge_object": None,
            }

        monkeypatch.setattr(app.state.ingestion, "ingest_file", _fake_ingest_file)

        response = client.post(
            "/api/chat",
            json={
                "message": "",
                "document": {
                    "filename": "voice-42.oga",
                    "content_base64": "T2dnUw==",
                    "media_kind": "voice",
                    "duration": 7,
                },
            },
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert response.status_code == 200, response.text

    assert captured["message"] == "С кем работал Иван Петров над проектом Аврора?", (
        "ходом разговора должен стать транскрипт, а не имя .oga-файла"
    )
    assert captured["synthetic"] is False, (
        "голосовой вопрос помечен как сгенерированное системой уведомление — "
        "он теряет графовый путь, который тот же вопрос текстом получает"
    )


@pytest.mark.asyncio
async def test_retry_of_a_generated_notice_stays_a_generated_notice(settings, monkeypatch):
    """«Ещё раз» не превращает строку, сочинённую backend'ом, в вопрос человека.

    `/api/me/regenerate` берёт СОХРАНЁННЫЙ текст последнего user-хода и зовёт
    `chat` заново. Признак «этот текст сгенерирован» жил только в памяти того
    запроса, где ход создавался, поэтому на повторе имя чужого файла с
    реляционной фразой («Загружен документ: с кем работал иван отчёт.pdf»)
    судилось классификатором как настоящий вопрос — и включало графовое
    расширение, которого первый ход не получал. При одном и том же тексте.

    Мутация: убрать `synthetic_document_notice=` из вызова `chat` в
    `/api/me/regenerate` (или перестать писать метку на ход) — тест обязан
    покраснеть.
    """
    from fastapi.testclient import TestClient

    from jericho.server import create_app

    app = create_app(settings)
    calls: list[dict] = []

    with TestClient(app) as client:
        real_search = app.state.hybrid_searcher.search

        async def _spy_search(user_id, query, **kwargs):
            calls.append({"query": query, **kwargs})
            return await real_search(user_id, query, **kwargs)

        monkeypatch.setattr(app.state.hybrid_searcher, "search", _spy_search)

        async def _fake_ingest_file(*_args, **_kwargs):
            return {"queued_for_review": True, "knowledge_object": None}

        monkeypatch.setattr(app.state.ingestion, "ingest_file", _fake_ingest_file)
        headers = {"Authorization": f"Bearer {settings.api_token}"}

        first = client.post(
            "/api/chat",
            json={
                "message": "",
                "document": {
                    "filename": "с кем работал иван отчет.pdf",
                    "content_base64": "JVBERi0=",
                },
            },
            headers=headers,
        )
        assert first.status_code == 200, first.text
        assert calls and calls[0].get("graph_expansion") is False, "первый ход уже пошёл не так"

        calls.clear()
        again = client.post(
            "/api/me/regenerate",
            json={"conversation_id": first.json()["conversation_id"]},
            headers=headers,
        )
        assert again.status_code == 200, again.text

    assert calls, "повтор не позвал поиск — проба проверяет не то"
    assert calls[0].get("graph_expansion") is False, (
        "на повторе сгенерированное уведомление о файле снова судится как вопрос человека"
    )
