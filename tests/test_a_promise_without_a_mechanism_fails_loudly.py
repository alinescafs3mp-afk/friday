"""Обещание, за которым нет механизма, не должно проходить молча."""

from __future__ import annotations

import pytest

from friday.permissions import CORE_CAPABILITIES, CapabilityDefinition


def test_marking_a_capability_as_needing_a_human_fails_loudly():
    """`default_requires_hitl` не читает НИКТО: авторизация знает только «можно»
    и «нельзя», а ядро после проверки права сразу зовёт обработчик.

    Пометить способность как требующую подтверждения и получить исполнение без
    подтверждения — хуже, чем не иметь пометки вовсе: автор уверен, что действие
    задержано. Пока шлюза нет, попытка обязана падать на старте.
    """
    marked = CapabilityDefinition(
        "test.dangerous", "Опасное действие", "test", 3, ("admin",), default_requires_hitl=True
    )
    with pytest.raises(ValueError, match="шлюза подтверждения"):
        marked.validate()


def test_no_core_capability_relies_on_the_missing_gate():
    for capability in CORE_CAPABILITIES:
        capability.validate()
    assert not [item for item in CORE_CAPABILITIES if item.default_requires_hitl]
