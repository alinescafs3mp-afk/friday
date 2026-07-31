"""Периодическая метрика качества обязана мерить то же, что видит владелец.

`_prepare_context` (главный путь агента) выключил расширение по графу 2026-07-31:
замерено, что оно уполовинивало recall@10 (0.35 -> 0.15). Но `_score_cases` — та же
функция, что кормит `run_eval`, а его периодически вызывает воркер и его же смотрит
`jericho doctor` — звала поиск БЕЗ `graph_expansion=False`. Значит официальная метрика
качества продолжала бы измерять СТАРОЕ, худшее поведение и молча разошлась бы с тем,
что владелец получает на самом деле: доктор показывал бы деградацию там, где её нет,
а будущее обнаружение регрессии сравнивало бы новые прогоны с неверной базой.

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
