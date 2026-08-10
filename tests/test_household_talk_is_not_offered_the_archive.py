"""На бытовой реплике модели не предлагают инструменты, читающие архив.

Заслон по вердикту «быт» выбрасывает найденные документы — но инструменты
оставались на месте, и модель добирала архив сама. Замерено на живой системе
2026-08-03, три прогона на каждом шаге:

    описания полные (вида «быт» в отборе не было)   лента звалась 3 раза из 3
    описания сокращены (вид добавлен)               2 раза из 3
    инструмент не предлагается вовсе                проверяется здесь

Ответ при этом получался пересказом рабочего дня человека — включая то, что писал
названный по имени коллега, — на реплику в два слова. Это ровно та жалоба, с
которой всё начиналось: «хочет человек поболтать, а она в архив лезет».

Почему это исключение из правила «набор не урезаем, только сокращаем описания».
Правило принято осознанно, и довод у него настоящий: «напомни завтра» посреди
болтовни — законный случай. Поэтому базовый бытовой профиль сохраняет `remind`,
чтобы явная просьба дошла до детерминированного prefetch. Но один семантический
вердикт «быт» не является полномочием на эффект: на обычной реплике runtime
снимает схему `remind` до основного хода модели. Здесь догадки нет: вердикт вынес
арбитр, и на ЭТОМ ЖЕ ходу по нему уже выброшены найденные документы. Оставлять
модели возможность добрать их инструментом — значит спорить с собой внутри
одного хода.

Поэтому у основной модели отнимается всё, чего текущая реплика явно не
разрешила: читатели архива и `remind`. Остальные бытовые способности остаются:
`speak`, `make_file`, `memory_save`.
"""

from __future__ import annotations

import pytest

from friday.execution_kernel import ExecutionKernel


@pytest.fixture
def kernel(settings, storage):
    from friday.knowledge_graph import KnowledgeGraph
    from friday.permissions import AuthorizationService

    instance = ExecutionKernel(AuthorizationService(storage), settings)
    instance.bind_services(storage, KnowledgeGraph(storage), None, None)
    return instance


@pytest.fixture
def owner_actor(storage):
    from friday.permissions import ActorContext

    storage.ensure_user("alice", preset_key="owner")
    return ActorContext(user_id="alice", preset_key="owner", source="test")


READS_THE_ARCHIVE = (
    "what_happened",
    "upcoming",
    "memory_search",
    "message_search",
    "kg_stats",
    "entity_lookup",
    "user_activity",
    "inbox_list",
)
STILL_USABLE_IN_CHAT = ("remind", "speak", "make_file", "memory_save")


def _names(kernel: ExecutionKernel, actor, *, topic: str) -> set[str]:
    return {
        str((tool.get("function") or {}).get("name") or "")
        for tool in kernel.get_tool_definitions(actor, topic=topic)
    }


@pytest.mark.parametrize("tool", READS_THE_ARCHIVE)
def test_household_talk_is_not_offered_archive_readers(kernel, owner_actor, tool: str) -> None:
    """Мутация: убрать `_WITHHELD_TOOLS` — лента снова на столе."""
    offered = _names(kernel, owner_actor, topic="быт")

    assert tool not in offered, f"на бытовой реплике модели предложен {tool}"


@pytest.mark.parametrize("tool", STILL_USABLE_IN_CHAT)
def test_what_the_rule_exists_for_is_untouched(kernel, owner_actor, tool: str) -> None:
    """«Напомни завтра» посреди болтовни — настоящий случай, ради него всё и затевалось.

    Если эта проверка краснеет, правка отняла способность, а не лишний соблазн.
    """
    everything = _names(kernel, owner_actor, topic="")
    household = _names(kernel, owner_actor, topic="быт")

    if tool not in everything:
        pytest.skip(f"{tool} недоступен этому актору и вне бытового вида")
    assert tool in household, f"бытовая реплика лишилась {tool}"


def test_an_archive_question_keeps_everything(kernel, owner_actor) -> None:
    """Отнимается ровно на бытовом вердикте и нигде больше."""
    archive = _names(kernel, owner_actor, topic="архив")

    for tool in READS_THE_ARCHIVE:
        if tool in _names(kernel, owner_actor, topic=""):
            assert tool in archive, f"вопрос к архиву лишился {tool}"


def test_an_unknown_verdict_takes_nothing_away(kernel, owner_actor) -> None:
    """Безопасное умолчание: неизвестный вид — полный набор, как было всегда."""
    everything = _names(kernel, owner_actor, topic="")

    assert _names(kernel, owner_actor, topic="невиданный вид") == everything


def test_small_talk_is_treated_as_household(settings, storage, kernel) -> None:
    """Асимметрия, из-за которой отнятие инструментов не работало вовсе.

    На реплику, которую система сама сочла разговором, вердикт у арбитра видов НЕ
    спрашивается — значит вид пуст, а пустой вид означает полные описания ВСЕХ
    инструментов. Получалось так: «привет» из закрытого списка не получало
    инструментов совсем, а распознанное пониманием «устал сегодня» — весь набор
    целиком, включая ленту событий.

    Замерено: лента звалась три раза из трёх; после сокращения описаний два из
    трёх; отнятие не действовало, потому что вид сюда не доезжал.

    Мутация: убрать подстановку `topic = "быт"` — тест краснеет.

    Проверяется ТО, ЧТО ПОЛУЧАЕТ ПОТРЕБИТЕЛЬ: поддельная модель записывает набор
    инструментов, который ей на самом деле предложили. Осмотр исходника здесь не
    годится вдвойне — он ломается от комментария и не заметил бы, что вид просто
    не доезжает до места, где его читают. Именно так этот дефект и прожил.
    """
    import asyncio

    from friday.agent_runtime import AgentRuntime
    from friday.permissions import ActorContext

    offered: list[str] = []

    class _LLM:
        """Отвечает как живая модель: сначала как арбитр, потом как собеседник."""

        enabled = True
        total_budget_sec = 30.0

        async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
            asked = " ".join(str(m.get("content") or "") for m in messages)
            # Арбитр коротких реплик. На живой системе он на эту фразу отвечает
            # «РАЗГОВОР» — и именно поэтому вид у хода не спрашивается вовсе.
            if "РАЗГОВОР или ЗАПРОС" in asked:
                return {"content": "РАЗГОВОР"}
            for tool in tools or []:
                offered.append(str((tool.get("function") or {}).get("name") or ""))
            return {"content": "Бывает. Отдохни."}

    storage.ensure_user("alice", preset_key="owner")
    agent = AgentRuntime(settings, storage, kernel=kernel)
    agent.llm = _LLM()
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    asyncio.run(agent.chat("alice", "устал сегодня", actor=actor))

    assert offered, "модель не получила инструментов вовсе — проверять нечего"
    for tool in ("what_happened", "memory_search", "user_activity"):
        assert tool not in offered, f"на бытовой реплике модели предложен {tool}"
    assert "remind" not in offered, "бытовой вердикт выдал модели полномочие на скрытый эффект"


def test_an_explicit_reminder_inside_short_household_speech_is_executed(
    settings, storage, kernel, monkeypatch
) -> None:
    """Явные слова о напоминании сохраняют способность до хода основной модели."""
    import asyncio

    from friday.agent_runtime import AgentRuntime
    from friday.execution_kernel import ToolResult
    from friday.permissions import ActorContext

    calls: list[tuple[str, dict[str, str]]] = []

    async def _execute(tool, params, actor=None):  # noqa: ANN001, ARG001
        calls.append((str(tool), dict(params)))
        assert tool == "remind"
        return ToolResult(
            "remind",
            True,
            {
                "created": True,
                "what": params["what"],
                "on": "2026-08-09",
                "at": "",
                "requested_when": params["when"],
                "delivery_scheduled": True,
            },
        )

    class _LLM:
        enabled = True
        total_budget_sec = 30.0

        async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
            asked = " ".join(str(item.get("content") or "") for item in messages)
            if "РАЗГОВОР или ЗАПРОС" in asked:
                return {"content": "РАЗГОВОР"}
            if "Реши, просят ли ПОСТАВИТЬ НАПОМИНАНИЕ" in asked:
                return {"content": ('{"напоминание":"да","что":"отдохнуть","когда":"завтра","остаток":""}')}
            raise AssertionError("явная просьба напомнить дошла до основной модели")

    storage.ensure_user("alice", preset_key="owner")
    monkeypatch.setattr(kernel, "execute", _execute)
    agent = AgentRuntime(settings, storage, kernel=kernel)
    agent.llm = _LLM()
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    response = asyncio.run(agent.chat("alice", "напомни завтра отдохнуть", actor=actor))

    assert response["tools_used"] == ["remind"]
    assert calls == [("remind", {"what": "отдохнуть", "when": "завтра"})]


def test_the_household_kind_also_shortens_descriptions(kernel, owner_actor) -> None:
    """Отнятое — не единственная мера: остальное описывается коротко.

    Полные описания всех инструментов — 4650 токенов в каждом вызове, а вызовов
    на ход несколько.
    """
    long_form = {
        str((tool.get("function") or {}).get("name")): len(
            str((tool.get("function") or {}).get("description") or "")
        )
        for tool in kernel.get_tool_definitions(owner_actor, topic="")
    }
    household = {
        str((tool.get("function") or {}).get("name")): len(
            str((tool.get("function") or {}).get("description") or "")
        )
        for tool in kernel.get_tool_definitions(owner_actor, topic="быт")
    }
    shortened = [name for name, size in household.items() if size < long_form.get(name, 0)]

    assert shortened, "на бытовой реплике описания остались полными"
