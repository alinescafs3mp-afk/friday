"""Веса каналов вынесены из выражения, и это НЕ должно ничего менять.

Подобрать вес, не имея возможности его изменить, нельзя, а править исходник на каждую
пробу значит мерить каждый раз другую программу. Поэтому появился шов — но у шва есть
цена: он превращает константу в переменную, и однажды кто-то (скорее всего я) поменяет
значение мимоходом. Тест держит две вещи: значения по умолчанию те же, что были в
выражении, и чужое имя веса отвергается, а не создаёт молча новый.
"""

from __future__ import annotations

import pytest

from jericho.retrieval import _CHANNEL_WEIGHTS, HybridSearcher


def test_the_defaults_are_the_constants_that_were_in_the_expression():
    """Числа из выражения до выноса: lexical 0.19, field 0.17, embedding 0.17, graph 0.16.

    Если одно из них меняется, это должно быть ОСОЗНАННОЙ правкой с замером на
    отложенной половине вопросов, а не побочным следствием чего-то ещё.
    """
    assert _CHANNEL_WEIGHTS == {
        "lexical": 0.19,
        "field": 0.17,
        "embedding": 0.17,
        "graph": 0.16,
    }


def test_a_searcher_without_overrides_uses_exactly_those(storage):
    searcher = HybridSearcher(storage, None)
    assert searcher._channel_weights == _CHANNEL_WEIGHTS  # noqa: SLF001
    # И это КОПИЯ: харнесс, поменявший свои веса, не должен править общий словарь.
    searcher._channel_weights["lexical"] = 99.0  # noqa: SLF001
    assert _CHANNEL_WEIGHTS["lexical"] == 0.19, "перезапись весов протекла в общий словарь"


def test_an_override_applies_only_to_that_searcher(storage):
    tuned = HybridSearcher(storage, None, channel_weights={"embedding": 0.5})
    assert tuned._channel_weights["embedding"] == 0.5  # noqa: SLF001
    assert tuned._channel_weights["lexical"] == 0.19, "переопределение одного веса задело другие"  # noqa: SLF001
    assert HybridSearcher(storage, None)._channel_weights == _CHANNEL_WEIGHTS  # noqa: SLF001


def test_an_unknown_weight_name_is_refused(storage):
    """Опечатка в имени не должна тихо не делать ничего — иначе замер соврёт."""
    with pytest.raises(ValueError, match="unknown channel weight"):
        HybridSearcher(storage, None, channel_weights={"lexcial": 0.3})


def test_a_negative_weight_is_clamped_rather_than_inverting_the_channel(storage):
    """Отрицательный вес превратил бы канал в штраф — это не «настройка», а другой поиск."""
    searcher = HybridSearcher(storage, None, channel_weights={"graph": -1.0})
    assert searcher._channel_weights["graph"] == 0.0  # noqa: SLF001
