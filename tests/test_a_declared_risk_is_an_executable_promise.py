"""Объявленный `risk="high"` обязан работать, а не украшать декларацию.

Разбор Codex (`sol/HARDENING_FOR_OPUS.md`, §13), проверенный мной на живом коде:
решение об одобрении читает ТОЛЬКО отдельный словарь `HIGH_RISK_TOOLS` по имени
инструмента. Поля `ToolSpec.risk` в этом пути нет вовсе.

Комментарий у самого поля обещает противоположное — и обещает справедливо:
«список опасных инструментов, живущий отдельно от них самих, — это fail-open:
новый инструмент, меняющий данные, по умолчанию не попадает под гейт, и никто об
этом не узнает, пока модель что-нибудь не сделает». Ровно это и происходит: поле
обязательно, забыть его нельзя, программа не соберётся — а на решение оно не
влияет.

Сегодня множества случайно совпадают: три инструмента с `risk="high"` есть в
словаре. Совпадение и есть вся защита. Следующий орган с опасным инструментом
объявит риск честно, в словарь не попадёт — и обойдёт человека, сделав всё «по
правилам».

Починка держит два обещания сразу:

    исполнение читает ДЕКЛАРАЦИЮ: объявлен `high`, предиката нет — закрываемся,
    то есть спрашиваем человека, а не исполняем;
    расхождение видно НА СТАРТЕ, а не при первом опасном вызове: инвариант
    доказывает полное соответствие словаря и деклараций.

Словарь при этом остаётся — риск живёт в аргументах, а не в инструменте
(`entity_merge_decide` с `decision=reject` безопасен, с `accept` — нет). Он
перестаёт быть ИСТОЧНИКОМ политики и становится её реализацией.
"""

from __future__ import annotations

import asyncio

import pytest

from friday.execution_kernel import HIGH_RISK_TOOLS, ExecutionKernel, ToolSpec
from friday.permissions import ActorContext, AuthorizationService


class _Stub:
    """Заглушка для служб, которых этот тест не касается.

    Путь заявки требует все четыре связанные службы, хотя пользуется одной. Без
    них ядро честно падает «services are not initialized» — и это ПРАВИЛЬНО:
    незаполненная зависимость не должна выглядеть как отказ инструмента.
    """


@pytest.fixture
def kernel(settings, storage):
    from friday.knowledge_graph import KnowledgeGraph

    instance = ExecutionKernel(AuthorizationService(storage), settings)
    instance.bind_services(storage, KnowledgeGraph(storage), _Stub(), _Stub())
    return instance


@pytest.fixture
def owner(storage):
    storage.ensure_user("alice", preset_key="owner")
    return ActorContext(user_id="alice", preset_key="owner", source="test")


def test_a_high_tool_outside_the_dictionary_still_asks_a_person(kernel, owner, storage) -> None:
    """Мутация: вернуть решение по одному словарю — инструмент снова исполнится молча.

    Синтетический инструмент объявляет `risk="high"` честно и в словаре
    отсутствует — ровно положение следующего органа, написанного по правилам.
    """
    done: list[str] = []

    async def _handler(actor=None, **kwargs):  # noqa: ANN001, ANN003, ARG001
        done.append("сделано")
        return {"ok": True}

    kernel.register(
        ToolSpec(
            name="synthetic_wipe",
            description="Стереть всё.",
            parameters={"type": "object", "properties": {}},
            security_id="knowledge.delete",
            risk="high",
            handler=_handler,
        )
    )
    assert "synthetic_wipe" not in HIGH_RISK_TOOLS, "проверка потеряла смысл: имя попало в словарь"

    result = asyncio.run(kernel.execute("synthetic_wipe", {}, actor=owner))

    assert done == [], "инструмент, объявленный опасным, исполнился без человека"
    assert result.success is False
    waiting = storage.list_action_approvals("alice")
    assert [row["tool"] for row in waiting] == ["synthetic_wipe"], "заявка человеку не заведена"


def test_an_observing_tool_is_not_dragged_into_approvals(kernel, owner) -> None:
    """Обратная сторона: правка, требующая человека на всё, останавливает работу."""
    seen: list[str] = []

    async def _handler(actor=None, **kwargs):  # noqa: ANN001, ANN003, ARG001
        seen.append("прочитано")
        return {"ok": True}

    kernel.register(
        ToolSpec(
            name="synthetic_peek",
            description="Просто посмотреть.",
            parameters={"type": "object", "properties": {}},
            security_id="knowledge.read",
            risk="observe",
            handler=_handler,
        )
    )

    result = asyncio.run(kernel.execute("synthetic_peek", {}, actor=owner))

    assert seen == ["прочитано"] and result.success is True


def test_the_dictionary_and_the_declarations_agree(kernel) -> None:
    """Расхождение обязано быть видно НА СТАРТЕ, а не при первом опасном вызове.

    Две стороны сразу: имя в словаре без объявленного `high` — забытая
    декларация; объявленный `high` без предиката — забытый предикат. Обе тихие,
    и обе означают, что политика разъехалась со своей реализацией.
    """
    kernel.assert_risk_declarations_agree()


def test_a_forgotten_predicate_is_caught_at_startup(kernel) -> None:
    """Мутация: убрать инвариант — расхождение доживёт до первого опасного вызова."""

    async def _handler(actor=None, **kwargs):  # noqa: ANN001, ANN003, ARG001
        return {"ok": True}

    kernel.register(
        ToolSpec(
            name="synthetic_burn",
            description="Сжечь.",
            parameters={"type": "object", "properties": {}},
            security_id="knowledge.delete",
            risk="high",
            handler=_handler,
        )
    )

    with pytest.raises(ValueError, match="synthetic_burn"):
        kernel.assert_risk_declarations_agree()


def test_a_dictionary_entry_without_a_declaration_is_caught_too(kernel) -> None:
    """Вторая сторона: предикат есть, а инструмент опасным себя не считает."""

    async def _handler(actor=None, **kwargs):  # noqa: ANN001, ANN003, ARG001
        return {"ok": True}

    kernel.register(
        ToolSpec(
            name="synthetic_mild",
            description="Почти безобидно.",
            parameters={"type": "object", "properties": {}},
            security_id="knowledge.create",
            risk="mutate",
            handler=_handler,
        )
    )
    HIGH_RISK_TOOLS["synthetic_mild"] = lambda _arguments: True
    try:
        with pytest.raises(ValueError, match="synthetic_mild"):
            kernel.assert_risk_declarations_agree()
    finally:
        HIGH_RISK_TOOLS.pop("synthetic_mild", None)


def test_a_revoked_right_stops_an_already_approved_action(kernel, owner, storage) -> None:
    """Настоящая повторная авторизация — вместо снятой бутафории `policy_epoch`.

    Тот механизм сравнивал `ActorContext.policy_epoch = 1` с единицей: значение
    было константой, которую не увеличивали ни выдача права, ни отзыв, ни смена
    пресета. Комментарии называли это защитой от смены политики, а storage-тест
    доказывал помощника, сравнивая переданные руками `epoch-1` и `epoch-2`.

    Защита при этом существовала — другая: `execute_approved` спрашивает
    `authorization.require()` НЕПОСРЕДСТВЕННО перед побочным эффектом. Здесь
    проверяется именно она, на настоящем контуре: право снимается ПОСЛЕ решения
    человека, и действие не исполняется.

    Мутация: убрать `require()` из `execute_approved` — тест краснеет.
    """
    done: list[str] = []

    async def _handler(actor=None, **kwargs):  # noqa: ANN001, ANN003, ARG001
        done.append("сделано")
        return {"ok": True}

    kernel.register(
        ToolSpec(
            name="synthetic_erase",
            description="Стереть.",
            parameters={"type": "object", "properties": {}},
            security_id="knowledge.delete",
            risk="high",
            handler=_handler,
        )
    )
    asyncio.run(kernel.execute("synthetic_erase", {}, actor=owner))
    waiting = storage.list_action_approvals("alice")
    assert waiting, "заявка не заведена — проверять нечего"
    approval_id = waiting[0]["id"]
    storage.decide_action_approval(
        approval_id, "alice", decision="approve", decided_by="alice", person_id="alice"
    )

    # Право снято ПОСЛЕ решения человека и ДО исполнения.
    storage.set_permission_override("alice", "knowledge.delete", "deny")

    result = asyncio.run(kernel.execute_approved(approval_id, actor=owner))

    assert done == [], "действие исполнилось после снятия права"
    assert result.success is False


def test_the_shipped_registry_is_consistent(settings, storage) -> None:
    """То же самое на НАСТОЯЩЕМ наборе инструментов, а не на синтетическом."""
    from friday.knowledge_graph import KnowledgeGraph

    instance = ExecutionKernel(AuthorizationService(storage), settings)
    instance.bind_services(storage, KnowledgeGraph(storage), None, None)

    instance.assert_risk_declarations_agree()
