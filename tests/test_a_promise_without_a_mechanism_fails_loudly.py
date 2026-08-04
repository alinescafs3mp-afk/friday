"""Обещание «требует человека» теперь обеспечено механизмом.

Пометка `default_requires_hitl` была обещанием без механизма: авторизация знала
только «можно» и «нельзя», ядро после проверки права сразу звало обработчик, и
пометка не задерживала ничего. Пока так было, попытка её выставить падала на
старте — это честнее молчания.

Шлюз с тех пор построен: заявка человеку с кнопками, отпечаток аргументов,
повторная проверка прав перед эффектом (`action_approvals`). Здесь проверяется,
что пометка ДЕЙСТВИТЕЛЬНО задерживает действие, а не снова висит украшением:
запрет снят ровно потому, что механизм появился, и тест обязан ловить обратное.
"""

from __future__ import annotations

import pytest

from friday.permissions import CORE_CAPABILITIES, AuthorizationService, CapabilityDefinition


def test_marking_a_capability_as_needing_a_human_is_allowed_now(storage):
    """Пометка больше не роняет старт — потому что её наконец читают."""

    marked = CapabilityDefinition(
        "test.dangerous", "Опасное действие", "test", 3, ("admin",), default_requires_hitl=True
    )

    marked.validate()  # не должно бросать


def test_the_mark_is_visible_to_whoever_asks(storage):
    """Ядро спрашивает авторизацию, а не догадывается по имени инструмента."""

    service = AuthorizationService(storage)
    service.register_capability(
        CapabilityDefinition(
            "test.dangerous",
            "Опасное действие",
            "test",
            3,
            ("admin",),
            default_requires_hitl=True,
            source="test",
        )
    )
    service.register_capability(
        CapabilityDefinition("test.plain", "Обычное", "test", 0, ("user",), source="test")
    )

    assert service.capability_requires_person("test.dangerous") is True
    assert service.capability_requires_person("test.plain") is False
    assert service.capability_requires_person("нет такой") is False


@pytest.mark.asyncio
async def test_a_marked_capability_delays_its_tool(settings, storage):
    """Главное свойство: помеченная способность задерживает ЛЮБОЙ свой инструмент.

    Класс риска инструмента говорит, что делает вызов; пометка говорит, что
    владелец не отдаёт этот класс действий без своего слова — даже если сам вызов
    выглядит безобидным.
    """

    from friday.execution_kernel import ExecutionKernel
    from friday.permissions import ActorContext

    storage.ensure_user("alice", preset_key="owner")
    service = AuthorizationService(storage)
    # Имя своё, а не системное: переписать чужое право нельзя, и это отдельный
    # страж — орган однажды так подменил `kg.merge` одной строкой.
    service.register_capability(
        CapabilityDefinition(
            "test.marked",
            "Опасный класс",
            "test",
            3,
            ("owner",),
            default_requires_hitl=True,
            source="test",
        )
    )
    kernel = ExecutionKernel(service, settings)
    ActorContext(user_id="alice", preset_key="owner", source="test", person_id="alice")

    assert kernel._capability_requires_person("test.marked") is True
    assert kernel._capability_requires_person("kg.read") is False


def test_no_core_capability_is_marked_by_accident():
    """Ни одна системная способность не помечена — пометка это решение владельца."""

    for capability in CORE_CAPABILITIES:
        capability.validate()
    assert not [item for item in CORE_CAPABILITIES if item.default_requires_hitl]
