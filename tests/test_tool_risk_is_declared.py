"""Класс риска объявляет сам инструмент, и забыть его нельзя (спека v3 §5).

Список опасных инструментов, живущий ОТДЕЛЬНО от инструментов, — это fail-open:
новый инструмент, меняющий данные, по умолчанию не попадает под гейт, и узнают об
этом, когда модель что-нибудь сделает. Поэтому `risk` — обязательное поле
`ToolSpec` без умолчания: программа не соберётся, пока класс не назван.

Этот файл — вторая половина той же защиты. Обязательное поле заставляет назвать
класс, но не мешает назвать его НЕВЕРНО: приписать `observe` инструменту, который
пишет, — и гейт снова обойдён, на этот раз тихо. Здесь список каждого класса
зафиксирован поимённо, и любое изменение требует осознанной правки теста.
"""

from __future__ import annotations

import pytest

from friday.execution_kernel import HIGH_RISK_TOOLS, POSTCONDITIONS, ExecutionKernel, ToolSpec
from friday.permissions import AuthorizationService

# Только читают. Ошибка в эту сторону дорога: инструмент, который на самом деле
# пишет, но объявлен наблюдателем, обходит и гейт подтверждения, и проверку
# постусловий.
OBSERVE = {
    "memory_search",
    "message_search",
    "web_search",
    "web_fetch",
    "web_research",
    "entity_lookup",
    "kg_stats",
    "what_happened",
    "list_tags",
    "resolve_duplicates",
    "conflict_list",
    "user_activity",
    "user_knowledge_search",
    "inbox_list",
}
# Меняют данные, но обратимо и в пределах своего арендатора: запись знания можно
# удалить, сущность — удалить или переименовать, слияние — откатить.
MUTATE = {
    "memory_save",
    "entity_create",
    "entity_link",
    "entity_merge_undo",
    "speak",
    "mission_propose",
}
# Необратимое, каноническое или исполняющее — только через человека.
HIGH = {"conflict_decide", "entity_merge_decide", "code_run"}


@pytest.fixture
def kernel(settings, storage):
    return ExecutionKernel(AuthorizationService(storage), settings)


def test_every_tool_declares_its_risk_class(kernel):
    unknown = {
        name: tool.risk
        for name, tool in kernel._tools.items()  # noqa: SLF001
        if tool.risk not in {"observe", "mutate", "high"}
    }
    assert not unknown, f"инструменты с неизвестным классом риска: {unknown}"


def test_the_risk_classes_are_exactly_these(kernel):
    """Мутация: приписать любому пишущему инструменту `observe` — тест краснеет.

    Списки поимённые, потому что молчаливое перемещение инструмента между
    классами — это ровно то изменение, которое должно требовать решения человека.
    """
    by_risk: dict[str, set[str]] = {"observe": set(), "mutate": set(), "high": set()}
    for name, tool in kernel._tools.items():  # noqa: SLF001
        by_risk.setdefault(tool.risk, set()).add(name)

    assert by_risk["observe"] == OBSERVE, (
        f"наблюдательные разошлись: лишние {by_risk['observe'] - OBSERVE}, "
        f"пропавшие {OBSERVE - by_risk['observe']}"
    )
    assert by_risk["mutate"] == MUTATE
    assert by_risk["high"] == HIGH


def test_a_tool_spec_cannot_be_built_without_a_risk_class():
    """Умолчания нет намеренно: забыть класс невозможно."""
    with pytest.raises(TypeError):
        ToolSpec(  # type: ignore[call-arg]
            name="whatever",
            description="",
            parameters={},
            security_id="kg.read",
        )


def test_the_gate_covers_exactly_the_high_risk_tools(kernel):
    """Объявленный класс и гейт не могут разойтись.

    Разойтись они могут двумя способами, и оба плохи по-своему: инструмент
    объявлен `high`, но предиката для него нет — значит гейт его пропускает
    (fail-open); предикат есть, а класс ниже — значит класс врёт тому, кто читает
    спецификацию.
    """
    declared_high = {name for name, tool in kernel._tools.items() if tool.risk == "high"}  # noqa: SLF001
    assert declared_high == set(HIGH_RISK_TOOLS), (
        f"объявлено high: {sorted(declared_high)}, под гейтом: {sorted(HIGH_RISK_TOOLS)}"
    )


def test_high_risk_tools_that_change_stored_state_are_verified(kernel):
    """У необратимого действия должна быть проверка постусловия — или причина её не иметь.

    `code_run` — единственное законное исключение: его результат и есть вывод
    программы, проверять в базе нечего. Всё остальное, что меняет хранимое
    состояние, обязано подтверждать факт чтением базы.
    """
    verified = set(POSTCONDITIONS)
    missing = HIGH - verified - {"code_run"}
    assert not missing, f"необратимые действия без проверки постусловия: {sorted(missing)}"
