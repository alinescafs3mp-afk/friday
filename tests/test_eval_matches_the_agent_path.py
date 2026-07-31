"""Периодическая метрика качества обязана мерить то же, что видит владелец.

`_prepare_context` (главный путь агента) выключает расширение для обычных запросов:
замерено, что там оно уполовинивало recall@10 (0.35 -> 0.15). На отдельном наборе из
12 реляционных кейсов канал дал допустимый net_gain=2 без сбоев, поэтому и боевой
путь, и `_score_cases` включают его только по одному классификатору.

Тест ПРОВОДОЧНЫЙ: подделывает `search` шпионом и проверяет, с чем его ЗВАЛИ, а не то,
что параметр `graph_expansion` где-то существует в сигнатуре.
"""

from __future__ import annotations

import pytest


class _SpySearcher:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def search(self, user_id, query, **kwargs):
        self.calls.append(kwargs)
        return {"results": []}


@pytest.mark.asyncio
async def test_score_cases_asks_for_no_graph_expansion(settings, storage):
    """Мутация, которую тест обязан ловить: убрать `graph_expansion=False` из вызова
    в `_score_cases`. По умолчанию True, поэтому дефект не упадёт нигде, кроме числа."""
    from jericho.eval import _score_cases

    storage.ensure_user("alice")
    storage.add_eval_case("alice", "тестовый вопрос", ["ko_1"])
    cases = storage.list_eval_cases("alice")

    spy = _SpySearcher()
    await _score_cases(spy, None, "alice", cases, 10)

    assert spy.calls, "боевой путь вообще не позвал поиск — проба проверяет не то"
    assert spy.calls[0].get("graph_expansion") is False, (
        "run_eval снова измерит поведение, которого владелец не видит: боевой путь "
        "выключает расширение по графу, а замер — нет"
    )


@pytest.mark.asyncio
async def test_score_cases_matches_relational_graph_mode():
    """Мутация: сделать флаг константой False — измерительный путь обязан упасть."""
    from jericho.eval import _score_cases

    spy = _SpySearcher()
    cases = [{"id": "case-1", "query": "с кем работал Альфа", "expected_ids": ["ko_1"]}]

    await _score_cases(spy, None, "alice", cases, 10)

    assert spy.calls
    assert spy.calls[0].get("graph_expansion") is True
