"""Собственный поиск агента отдавал документы ЦЕЛИКОМ и обрезался на 12 000 знаках.

Замерено на архиве владельца: средняя длина документа 16 565 знаков — то есть ОДИН
средний документ переполняет весь бюджет инструмента; 231 документ длиннее самого
бюджета, самый длинный 1 344 266 знаков. На реальных запросах до модели доходил один
результат из десяти.

Хуже: в ответе `results` шёл раньше `count`, поэтому обрезка съедала и счётчик —
модель не видела даже, сколько было найдено.

И обрезалась ГОЛОВА документа, а не совпавший фрагмент. Для контекстного пути это уже
чинили («Quote the passage that matched, not the top of the document»), до инструмента
памяти починка не дошла.

Косвенное подтверждение, что этим путём не пользовались: в аудите живой базы 2860
записей и НИ ОДНОЙ `tool.invoke` за 15 диалогов.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from jericho.execution_kernel import ExecutionKernel, ToolResult
from jericho.storage.models import KnowledgeObject, RawObject, new_id


def _make(storage, user_id: str, index: int, *, head: str, tail: str) -> str:
    """Документ, где искомое стоит в КОНЦЕ: голова его не содержит."""
    text = head * 400 + " " + tail
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="t",
        source_ref=new_id("s"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(f"{index}".encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=f"Документ {index}",
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


@pytest.fixture
def kernel(settings, storage):
    from jericho.knowledge_graph import KnowledgeGraph
    from jericho.permissions import AuthorizationService

    instance = ExecutionKernel(AuthorizationService(storage), settings)
    instance.bind_services(storage, KnowledgeGraph(storage), None, None)
    return instance


def _search(kernel, storage, user_id: str, query: str, limit: int = 10) -> dict:
    from jericho.permissions import ActorContext

    actor = ActorContext(user_id=user_id, preset_key="owner", source="test")
    return asyncio.run(kernel._memory_search(actor=actor, query=query, limit=limit))  # noqa: SLF001


def test_the_result_carries_excerpts_not_whole_documents(kernel, storage):
    """Один средний документ этого архива переполняет бюджет инструмента целиком."""
    storage.ensure_user("alice")
    for index in range(5):
        _make(storage, "alice", index, head="наполнитель ", tail="сведения о поверке весов")

    found = _search(kernel, storage, "alice", "поверке")
    assert found["results"], "ничего не нашлось — проба не проверяет то, ради чего написана"
    for item in found["results"]:
        assert "content" not in item, "тело документа снова уехало модели целиком"
        assert len(item["excerpt"]) <= 700

    encoded = json.dumps(found, ensure_ascii=False, indent=2)
    assert len(encoded) < 12_000, f"ответ инструмента снова не влезает в бюджет: {len(encoded)}"


def test_the_excerpt_shows_the_match_not_the_head_of_the_document(kernel, storage):
    """Починка «цитируй совпавшее, а не начало» до этого инструмента не доходила."""
    storage.ensure_user("alice")
    _make(storage, "alice", 0, head="посторонний текст ", tail="решение о поверке весов принято")

    found = _search(kernel, storage, "alice", "поверке")
    excerpt = found["results"][0]["excerpt"]
    assert "поверке" in excerpt, f"в выдержке нет искомого: {excerpt[:120]!r}"


def test_the_count_survives_truncation(kernel, storage):
    """`results` шёл раньше `count`, и обрезка съедала счётчик.

    Модель не видела даже, сколько было найдено, — то есть не могла сказать «нашлось
    десять, показываю три».
    """
    storage.ensure_user("alice")
    for index in range(4):
        _make(storage, "alice", index, head="текст ", tail="поверка весов")

    found = _search(kernel, storage, "alice", "поверка")
    assert list(found)[0] == "count", "счётчик снова не первый — обрезка его срежет"

    encoded = json.dumps(found, ensure_ascii=False, indent=2)
    assert '"count"' in encoded[:200], "счётчик не попадает в первые байты ответа"


def test_a_truncated_result_still_announces_itself(settings):
    """Если ответ всё-таки не влез, модель обязана это видеть."""
    result = ToolResult(tool_name="memory_search", success=True, data={"x": "я" * 20_000})
    message = result.to_llm_message()
    assert "truncated" in message


def test_the_hybrid_searcher_is_used_when_bound(settings, storage):
    """У инструмента был СВОЙ поиск: FTS по префиксу и LIKE, без эмбеддингов.

    Замерено на живой базе: «поставка» находит 0 документов, «поставк» — 2; «отчет» —
    13, «отчёт» — 3. Слово в именительном падеже — ровно так его напишет модель,
    переформулируя вопрос, — давало пустую выдачу при существующих документах.
    """
    from jericho.knowledge_graph import KnowledgeGraph
    from jericho.permissions import ActorContext, AuthorizationService

    class _Searcher:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def search(self, user_id, query, **kwargs):
            self.calls.append(query)
            return {"results": [{"id": "ko_1", "title": "Из гибридного", "content": "тело про поставку"}]}

    searcher = _Searcher()
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, KnowledgeGraph(storage), None, None, searcher=searcher)
    storage.ensure_user("alice")

    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    found = asyncio.run(kernel._memory_search(actor=actor, query="поставка", limit=5))  # noqa: SLF001

    assert searcher.calls == ["поставка"], "гибридный поиск не был вызван"
    assert found["results"][0]["title"] == "Из гибридного"


def test_without_a_searcher_it_still_works(settings, storage):
    """Ядро может быть собрано без поиска (тесты, CLI) — инструмент не должен падать."""
    storage.ensure_user("alice")
    from jericho.knowledge_graph import KnowledgeGraph
    from jericho.permissions import ActorContext, AuthorizationService

    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, KnowledgeGraph(storage), None, None)
    _make(storage, "alice", 0, head="текст ", tail="поверка весов")

    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    found = asyncio.run(kernel._memory_search(actor=actor, query="поверка", limit=5))  # noqa: SLF001
    assert found["count"] >= 0


# --- «нашлось, но ни одно не отвечает» ----------------------------------------


def _searcher_returning(rows, strategy):
    class _Fake:
        async def search(self, user_id, query, **kwargs):
            return {"results": list(rows), "strategy": dict(strategy)}

    return _Fake()


def test_the_agent_learns_that_candidates_were_filtered_out(kernel, storage):
    """«В архиве этого нет» и «нашлось двадцать, ни одно не о том» — разные ответы.

    Порог переранжировщика отбирает молча. Без этого числа модель выдаёт первый ответ
    в обоих случаях, хотя во втором похожее в архиве есть и человек его помнит.
    """
    kernel.searcher = _searcher_returning([], {"reranked": 20, "rerank_dropped": 20})

    found = _search(kernel, storage, "alice", "поверка весов")

    assert found["count"] == 0
    assert found["filtered_out"] == 20


def test_the_filtered_out_counter_precedes_the_results(kernel, storage):
    """`filtered_out` добавлялся ПОСЛЕ results — и обрезка длинного ответа съедала
    его точно так же, как когда-то съедала count. Счётчики стоят до выдержек."""
    rows = [{"id": f"ko_{index}", "title": "Док", "content": "тело"} for index in range(3)]
    kernel.searcher = _searcher_returning(rows, {"reranked": 20, "rerank_dropped": 17})

    found = _search(kernel, storage, "alice", "поверка весов")

    keys = list(found)
    assert keys.index("filtered_out") < keys.index("results"), "счётчик отсева позади выдержек"


def test_the_chat_model_is_told_similar_records_were_cut(settings, storage):
    """Главный путь чата: «в архиве пусто» и «похожее есть, но не отвечает» —
    разные ответы человеку, и без этой строки модель выдаёт первый в обоих
    случаях."""
    from jericho.agent_runtime import AgentContext, AgentRuntime

    runtime = AgentRuntime(settings, storage)
    context = AgentContext(
        conversation_id="conv",
        user_id="alice",
        answer_mode="personal_knowledge_missing",
        rerank_dropped=5,
    )
    messages = runtime._build_initial_messages(context, "какой номер у Иванова?", None, tool_enabled=False)  # noqa: SLF001

    joined = "\n".join(str(item.get("content")) for item in messages if item.get("role") == "system")
    assert "отсеяны порогом" in joined
    assert "5" in joined

    silent = AgentContext(conversation_id="conv", user_id="alice", answer_mode="personal_knowledge_missing")
    messages = runtime._build_initial_messages(silent, "какой номер у Иванова?", None, tool_enabled=False)  # noqa: SLF001
    joined = "\n".join(str(item.get("content")) for item in messages if item.get("role") == "system")
    assert "отсеяны порогом" not in joined


def test_rerank_top_without_endpoint_is_a_config_error(settings):
    """Включённый JERICHO_RERANK_TOP без адреса/модели — противоречие того же
    класса, что эмбеддинги без модели: и переранжирование, и порог уверенности
    молча не работают, пока настройка говорит «включено»."""
    import dataclasses

    from jericho.config import validate_settings

    broken = dataclasses.replace(settings, rerank_top=20, rerank_base_url="", rerank_model="")
    problems = validate_settings(broken)
    assert any("JERICHO_RERANK_TOP" in item and "warning" not in item for item in problems)

    configured = dataclasses.replace(
        settings, rerank_top=20, rerank_base_url="http://rerank.invalid/v1", rerank_model="qwen3"
    )
    assert not any("JERICHO_RERANK_TOP" in item for item in validate_settings(configured))


def test_nothing_extra_is_said_when_nothing_was_filtered(kernel, storage):
    """Пустой архив по теме — это по-прежнему просто ноль, без приписок."""
    kernel.searcher = _searcher_returning([], {"reranked": 0})

    found = _search(kernel, storage, "alice", "поверка весов")

    assert found["count"] == 0
    assert "filtered_out" not in found
