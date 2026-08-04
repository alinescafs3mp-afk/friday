"""Механизм остановки миссии работает только когда предел кто-то задал.

Аппарат был написан целиком и не сработал ни разу. Столбцы `budget_seconds`,
`budget_tool_calls`, `budget_retries` и `deadline_at` существовали в схеме;
`_budget_verdict` их читал и умел объяснить человеку причину словами; расход
исправно рос через `add_mission_spend`. Не было ровно одного звена: полей в самой
модели `Mission` — и потому при создании в каждой миссии оставались нули, а ноль
эта проверка трактует как «без ограничения».

На живой базе замерено: единственная миссия имеет 0/0/0 и пустой срок.

Тот же класс, что мёртвая кнопка компенсации сутками раньше: обещание, у которого
нет механизма, опаснее отсутствия обещания — на него ссылаются и считают, что
защита есть. Здесь механизм был, не было того, кто им пользуется.

Числа щедрые намеренно: предел отсекает зациклившуюся миссию, а не долгую. Полный
план — 12 шагов по 6 вызовов, то есть 72; исчерпать три сотни может только работа,
идущая по кругу.
"""

from __future__ import annotations

import json

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.executive import ExecutiveService
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.web_surfer import WebSurfer


class _Planner:
    enabled = True
    model = "stub"

    async def chat(self, messages, **kwargs):  # noqa: ANN003, ARG002
        if "планировщик миссий" in str(messages[0].get("content") or ""):
            return {
                "content": json.dumps(
                    {
                        "title": "План",
                        "tasks": [
                            {
                                "seq": 1,
                                "kind": "gather",
                                "title": "Шаг",
                                "instruction": "Сделай",
                                "depends_on": [],
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            }
        return {"content": "готово"}


@pytest.fixture
def executive(settings, storage):
    storage.ensure_user("alice", preset_key="owner")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(settings, storage, graph)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), ingestion)
    service = ExecutiveService(settings, storage, auth, kernel, _Planner(), ingestion)
    kernel.bind_executive(service)
    return service


@pytest.mark.asyncio
async def test_a_new_mission_carries_its_limits(executive, settings):
    """Мутация: убрать поля бюджета из `create_mission` — тест краснеет."""
    mission = await executive.create_mission("alice", "разобрать почту", created_by="alice")

    assert int(mission["budget_seconds"]) == settings.mission_budget_seconds > 0
    assert int(mission["budget_tool_calls"]) == settings.mission_budget_tool_calls > 0
    assert int(mission["budget_retries"]) == settings.mission_budget_retries > 0
    assert mission["deadline_at"], "срок не проставлен — миссия может идти вечно"


@pytest.mark.asyncio
async def test_an_exhausted_budget_stops_the_mission_with_a_reason(executive, storage):
    """Причина доезжает до человека словами, а не «миссия заблокирована».

    Мутация: вернуть `if budget <= 0: continue` для заданного бюджета — тест
    краснеет, потому что работа продолжится молча.
    """
    mission = await executive.create_mission("alice", "бесконечная работа", created_by="alice")

    storage.add_mission_spend(mission["id"], "alice", tool_calls=10_000)
    fresh = storage.get_mission(mission["id"], "alice")

    verdict = executive._budget_verdict(fresh)  # noqa: SLF001

    assert verdict, "исчерпанный бюджет не остановил миссию"
    assert "вызовы инструментов" in verdict, f"причина не названа: {verdict!r}"
    assert "10000" in verdict.replace(" ", ""), "не сказано, сколько потрачено"


@pytest.mark.asyncio
async def test_a_fresh_mission_is_not_stopped(executive, storage):
    """Ошибка в другую сторону: тесный предел остановил бы нормальную работу.

    Полный план — 12 шагов по 6 вызовов, то есть 72; расход в сотню вызовов
    и полчаса работы обязан проходить свободно.
    """
    mission = await executive.create_mission("alice", "обычная работа", created_by="alice")
    storage.add_mission_spend(mission["id"], "alice", seconds=1800, tool_calls=100, retries=5)

    assert executive._budget_verdict(storage.get_mission(mission["id"], "alice")) == ""  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_zero_budget_still_means_no_limit(settings, storage, monkeypatch):
    """Ноль остаётся выключателем: «без бюджета» и «нулевой бюджет» — разное.

    Настройки заморожены (frozen dataclass), поэтому здесь собирается служба с
    пределами, выключенными через окружение, — тем же путём, каким их выключил бы
    владелец.
    """
    from friday.config import load_settings

    for name in (
        "FRIDAY_MISSION_BUDGET_SECONDS",
        "FRIDAY_MISSION_BUDGET_TOOL_CALLS",
        "FRIDAY_MISSION_BUDGET_RETRIES",
        "FRIDAY_MISSION_DEADLINE_HOURS",
    ):
        monkeypatch.setenv(name, "0")
    unlimited = load_settings()

    storage.ensure_user("alice", preset_key="owner")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(unlimited, storage, graph)
    kernel = ExecutionKernel(auth, unlimited)
    kernel.bind_services(storage, graph, WebSurfer(unlimited), ingestion)
    service = ExecutiveService(unlimited, storage, auth, kernel, _Planner(), ingestion)

    mission = await service.create_mission("alice", "без пределов", created_by="alice")
    storage.add_mission_spend(mission["id"], "alice", tool_calls=99_999)

    assert int(mission["budget_tool_calls"]) == 0
    assert not mission["deadline_at"]
    assert service._budget_verdict(storage.get_mission(mission["id"], "alice")) == ""  # noqa: SLF001
