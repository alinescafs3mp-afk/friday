"""Правило одного человека не становится правилом всех.

Найдено разбором Сола 2026-08-03 — через коммит после того, как я закрыла ровно
этот класс в заявках на подтверждение. Указания о поведении и поправки писались по
`context.user_id`, а в общем архиве это АРЕНДАТОР, один на всех. «Отвечай мне
кратко», сказанное одним участником, легло бы правилом для остальных.

Хуже всего то, КАК я это проверила в первый раз. Прочитала код, увидела в `chat()`
строку `user_id = person_id` и заключила «всё в порядке». Строка есть и она про
другое: в `_prepare_context` намеренно передаётся ИМЕННО арендатор — искать надо в
том архиве, который человеку открыт, а в общем режиме это общий корпус. Контекст
получал арендатора, и личные списки шли туда же.

Показал ОПЫТ, а не чтение: правило легло в учётку арендатора, а у обоих людей
осталось пусто.

Поэтому у контекста теперь два идентификатора, и это не дублирование:

    user_id   — арендатор, по нему ищут в архиве;
    person_id — человек, по нему хранятся указания, поправки и авторство.

При обычной настройке они совпадают, и правка не может ничего сломать у тех, у
кого один пользователь.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime
from friday.permissions import ActorContext


class _Obedient:
    """Модель, послушно объявляющая сказанное правилом."""

    enabled = True
    total_budget_sec = 5.0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        asked = " ".join(str(m.get("content") or "") for m in messages)
        if "РАЗГОВОР или ЗАПРОС" in asked:
            return {"content": "ЗАПРОС"}
        if '"вид"' in asked:
            return {
                "content": '{"вид": "правило", "правило": "отвечать кратко",'
                ' "запрос": "", "кто": "", "дни": []}'
            }
        if "прежнее" in asked:
            return {"content": '{"действие": "запомнить", "правило": "отвечать кратко", "прежнее": 0}'}
        return {"content": "Поняла."}


def _rules_of(storage, user_id: str) -> list[str]:
    row = storage.execute("SELECT metadata_json FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return []
    meta = json.loads(str(row["metadata_json"] or "{}"))
    return list(meta.get("standing_rules") or []) if isinstance(meta, dict) else []


def test_a_rule_lands_on_the_person_not_the_tenant(settings, storage) -> None:
    """Мутация: вернуть `context.user_id` в хранение — правило снова у арендатора.

    Проверяются ВСЕ ТРИ учётки сразу, и это существенно: проверка «у сказавшего
    появилось» прошла бы и на сломанном коде, если бы человек и арендатор
    совпадали. Дыру показывает именно строка арендатора.
    """
    for name in ("tenant", "person-a", "person-b"):
        storage.ensure_user(name)
    agent = AgentRuntime(settings, storage)
    agent.llm = _Obedient()
    actor = ActorContext(
        user_id="tenant", preset_key="user", source="test", shared_tenant=True, person_id="person-a"
    )

    asyncio.run(agent.chat("tenant", "отвечай мне кратко", actor=actor, enable_tools=False))

    assert _rules_of(storage, "person-a") == ["отвечать кратко"], "правило не попало к сказавшему"
    assert _rules_of(storage, "person-b") == [], "правило одного стало правилом другого"
    assert _rules_of(storage, "tenant") == [], "правило легло в общую учётку арендатора"


def test_the_search_still_goes_to_the_shared_corpus(settings, storage) -> None:
    """Обратная сторона: правка не имеет права увести поиск в личную учётку.

    Общий архив для того и существует, чтобы люди видели документы друг друга.
    Контекст обязан искать по арендатору и хранить личное по человеку.
    """
    for name in ("tenant", "person-a"):
        storage.ensure_user(name)
    seen: list[str] = []

    class _Searcher:
        async def search(self, user_id, query, **kwargs):  # noqa: ANN001, ARG002
            seen.append(str(user_id))
            return {"results": [], "entity_matches": [], "strategy": "hybrid", "trace": []}

    agent = AgentRuntime(settings, storage)
    agent.llm = _Obedient()
    actor = ActorContext(
        user_id="tenant", preset_key="user", source="test", shared_tenant=True, person_id="person-a"
    )

    asyncio.run(
        agent.chat(
            "tenant", "что там по поверке приборов", actor=actor,
            enable_tools=False, hybrid_searcher=_Searcher(),
        )
    )

    assert seen == ["tenant"], f"поиск ушёл не в общий корпус: {seen}"


def test_without_a_shared_archive_both_ids_are_the_same(settings, storage) -> None:
    """Установка с одним пользователем не может пострадать от этой правки."""
    storage.ensure_user("solo", preset_key="owner")
    agent = AgentRuntime(settings, storage)
    agent.llm = _Obedient()
    actor = ActorContext(user_id="solo", preset_key="owner", source="test")

    asyncio.run(agent.chat("solo", "отвечай мне кратко", actor=actor, enable_tools=False))

    assert _rules_of(storage, "solo") == ["отвечать кратко"]


@pytest.mark.parametrize("field", ["standing_rules", "corrections"])
def test_both_personal_lists_are_read_by_person(settings, storage, field: str) -> None:
    """Поправки — тот же класс данных, что и правила, и та же граница."""
    storage.ensure_user("tenant")
    storage.ensure_user("person-a")
    if field == "standing_rules":
        storage.remember_standing_rule("person-a", "не ставить смайлики")
    else:
        storage.remember_correction("person-a", "День морской пехоты — 27 ноября")
    agent = AgentRuntime(settings, storage)

    context = AgentContext(
        conversation_id="c", user_id="tenant", person_id="person-a", search_query=""
    )
    messages = agent._build_initial_messages(context, "", None, tool_enabled=False)

    data = [m["content"] for m in messages if m.get("role") == "user"]
    assert data, "личный список не поднял блок контекста"
    assert ("смайлики" in data[0]) or ("27 ноября" in data[0])


def test_the_context_keeps_both_identities() -> None:
    """Механизм без обоих идентификаторов не отличит корпус от человека."""
    context = AgentContext(conversation_id="c", user_id="tenant", person_id="person-a")

    assert context.user_id == "tenant"
    assert context.person_id == "person-a"
