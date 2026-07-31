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
